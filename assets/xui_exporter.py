#!/usr/bin/env python3
"""3x-ui 3.3/3.5 API metrics and persistent Xray access-log forwarding.

Legacy XUI_API_URL deployments still expose metrics on demand. A panels file
enables background polling so slow remote panels cannot block Prometheus.
"""

import argparse
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


API_URL = os.environ.get("XUI_API_URL", "").rstrip("/")
TOKEN_FILE = os.environ.get("XUI_API_TOKEN_FILE", "/run/secrets/xui_api_token")
PANELS_FILE = os.environ.get("XUI_PANELS_FILE", "")
STATE_DIR = os.environ.get("XUI_STATE_DIR", "/var/lib/xui")
LOKI_URL = os.environ.get("XUI_LOKI_URL", "http://alloy:3500/loki/api/v1/push")
TIMEOUT = float(os.environ.get("XUI_API_TIMEOUT", "10"))
SERVER_NAME = os.environ.get("SERVER_NAME", "").strip() or "unknown"
NAME_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,62}\Z")
ERRORS = (OSError, RuntimeError, ValueError, TypeError, KeyError, sqlite3.Error)
METRICS = {
    "xui_exporter_up": ("gauge", "3x-ui traffic and online API health"),
    "xui_metrics_last_success_timestamp_seconds": ("gauge", "Last successful metrics poll"),
    "xui_client_traffic_bytes_total": ("counter", "Client traffic bytes"),
    "xui_inbound_traffic_bytes_total": ("counter", "Inbound traffic bytes"),
    "xui_client_online": ("gauge", "Recently active clients reported by 3x-ui"),
    "xui_client_last_online_timestamp_seconds": ("gauge", "Client last online timestamp"),
    "xui_access_log_enabled": ("gauge", "API log collection enabled"),
    "xui_access_log_up": ("gauge", "Last API log poll and forwarding health"),
    "xui_access_log_last_success_timestamp_seconds": ("gauge", "Last successful log poll"),
    "xui_access_log_window_full": ("gauge", "API returned the entire requested log window"),
    "xui_access_log_window_gap": ("gauge", "Full log window without overlap with previous poll"),
    "xui_access_log_invalid_records": ("gauge", "Unusable records in last API log response"),
}


def metric_escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def labels(**values):
    return ",".join(f'{key}="{metric_escape(value)}"' for key, value in values.items())


def metric(name, value, **tags):
    return f"{name}{{{labels(**tags)}}} {value}"


def headers():
    return [line for name, (kind, help_text) in METRICS.items()
            for line in (f"# HELP {name} {help_text}", f"# TYPE {name} {kind}")]


def validate_panel(panel):
    if not isinstance(panel, dict) or not NAME_RE.fullmatch(panel.get("name", "")):
        raise ValueError("面板名称只能包含字母、数字、点、下划线和连字符，最多 63 字符")
    url = panel.get("url", "")
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or any(c.isspace() for c in url)
            or not parsed.path.endswith("/panel/api/inbounds/list")):
        raise ValueError("面板地址必须是完整 HTTPS 地址，以 /panel/api/inbounds/list 结尾")
    if not isinstance(panel.get("logs", False), bool):
        raise ValueError("logs 必须是 true 或 false")
    token_file = panel.get("token_file", "")
    if not isinstance(token_file, str) or not token_file.startswith("/run/secrets/") or "/" in token_file[len("/run/secrets/"):]:
        raise ValueError("Token 文件必须位于 /run/secrets/ 目录")
    for key, default, minimum, maximum in (("interval", 30, 10, 3600),
                                           ("log_count", 1000, 100, 10000),
                                           ("max_log_age", 3600, 60, 86400)):
        value = panel.get(key, default)
        if type(value) is not int or not minimum <= value <= maximum:
            raise ValueError(f"{key} 必须是 {minimum} 到 {maximum} 的整数")
    return panel


def load_config(path):
    if not Path(path).exists():
        return []
    panels = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(panels, list):
        raise ValueError("面板配置必须是 JSON 数组")
    names = set()
    for panel in panels:
        validate_panel(panel)
        if panel["name"] in names:
            raise ValueError("面板名称不能重复")
        names.add(panel["name"])
    return panels


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".xui-", delete=False) as output:
            temporary = output.name
            os.chmod(temporary, 0o600)
            output.write(text)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def read_token(path=None):
    token = Path(path or TOKEN_FILE).read_text(encoding="utf-8").strip()
    if not token or "\n" in token or "\r" in token:
        raise ValueError("3x-ui Token 不能为空或包含换行符")
    return token


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, response_headers, new_url):
        # Never forward panel tokens to a login page or another origin.
        return None


HTTP = urllib.request.build_opener(NoRedirect())


def request_json(url, method="GET", token=None, form=None):
    data = urllib.parse.urlencode(form).encode() if form is not None else (b"" if method == "POST" else None)
    request = urllib.request.Request(url, data=data, method=method, headers={
        "Authorization": f"Bearer {token if token is not None else read_token()}",
        "Accept": "application/json", "X-Requested-With": "XMLHttpRequest",
        "Content-Type": "application/x-www-form-urlencoded",
        "User-Agent": "gla-xui-exporter/2.0",
    })
    with HTTP.open(request, timeout=TIMEOUT) as response:
        payload = json.load(response)
    if not isinstance(payload, dict) or payload.get("success") is not True:
        # Server messages can echo credentials. Keep diagnostics local and safe.
        raise RuntimeError("3x-ui API 返回 success=false 或无效响应")
    return payload.get("obj")


def endpoint(api_url, suffix):
    if not api_url.endswith("/inbounds/list"):
        raise ValueError("XUI_API_URL 必须指向 /panel/api/inbounds/list")
    return api_url[:-len("/inbounds/list")] + suffix


def online_url():
    return endpoint(API_URL, "/clients/onlines")


def render_metrics(server, inbounds, online_emails, clients=None):
    if not isinstance(inbounds, list) or not isinstance(online_emails, list):
        raise ValueError("入站或在线列表响应类型不正确")
    online = set(online_emails)
    lines = [metric("xui_exporter_up", 1, server=server),
             metric("xui_metrics_last_success_timestamp_seconds", time.time(), server=server)]
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            raise ValueError("入站记录响应类型不正确")
        tags = {"server": server, "inbound_id": inbound.get("id", 0),
                "inbound": inbound.get("remark") or inbound.get("tag") or "unnamed",
                "port": inbound.get("port", 0), "protocol": inbound.get("protocol") or "unknown"}
        for direction, key in (("uplink", "up"), ("downlink", "down")):
            lines.append(metric("xui_inbound_traffic_bytes_total", int(inbound.get(key) or 0), direction=direction, **tags))

    # 3.x exposes globally shared client traffic; counting it per attachment
    # duplicates bytes and online users for clients on multiple inbounds.
    if clients is None:
        records = {c["email"]: c for inbound in inbounds for c in inbound.get("clientStats") or [] if c.get("email")}
    else:
        if not isinstance(clients, list) or any(not isinstance(c, dict) for c in clients):
            raise ValueError("客户端列表响应类型不正确")
        records = {c["email"]: (c.get("traffic") or {}) for c in clients if c.get("email")}
    for email in sorted(set(records) | online):
        traffic = records.get(email, {})
        if not isinstance(traffic, dict):
            raise ValueError("客户端流量响应类型不正确")
        tags = {"server": server, "email": email}
        if email in records:
            for direction, key in (("uplink", "up"), ("downlink", "down")):
                lines.append(metric("xui_client_traffic_bytes_total", int(traffic.get(key) or 0), direction=direction, **tags))
            lines.append(metric("xui_client_last_online_timestamp_seconds", f'{int(traffic.get("lastOnline") or 0) / 1000:.3f}', **tags))
        lines.append(metric("xui_client_online", int(email in online), **tags))
    return lines


def collect():
    """Compatibility mode for existing single-panel collectors (no log push)."""
    inbounds = request_json(API_URL)
    online = request_json(online_url(), "POST")
    return "\n".join(headers() + render_metrics(SERVER_NAME, inbounds or [], online or [])) + "\n"


def timestamp_ns(value):
    match = re.fullmatch(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})(?:\.(\d{1,9}))?(Z|[+-]\d{2}:\d{2})", value)
    if not match:
        raise ValueError("访问记录时间戳无效")
    date = datetime.fromisoformat(match[1] + match[3].replace("Z", "+00:00"))
    seconds = (date.astimezone(timezone.utc) - datetime(1970, 1, 1, tzinfo=timezone.utc)).total_seconds()
    return int(seconds) * 1_000_000_000 + int((match[2] or "").ljust(9, "0"))


def normalize_log(entry, server):
    ns = timestamp_ns(entry.get("DateTime", ""))
    source, target = entry.get("FromAddress", ""), entry.get("ToAddress", "")
    inbound, outbound, email = (entry.get(k) or "" for k in ("Inbound", "Outbound", "Email"))
    if not source or not target or "api -> api" in f"{inbound} -> {outbound}":
        raise ValueError("访问记录缺少来源或目标")
    if any(any(char.isspace() for char in v) for v in (source, target, inbound, outbound, email)):
        raise ValueError("访问记录字段含空白字符")
    seconds, fraction = divmod(ns, 1_000_000_000)
    date = datetime.fromtimestamp(seconds, timezone(timedelta(hours=8)))
    line = f'{date:%Y/%m/%d %H:%M:%S}.{fraction:09d} from {source} accepted {target} [{inbound} -> {outbound}]'
    if email:
        line += f" email: {email}"
    tags = {"job": "xray-access", "server": server, "collector": "3x-ui-api"}
    for key, value in (("email", email), ("inbound", inbound), ("outbound", outbound)):
        if value:
            tags[key] = value
    digest = hashlib.sha256(json.dumps([ns, source, target, inbound, outbound, email, entry.get("Event")], ensure_ascii=False).encode()).hexdigest()
    return digest, ns, tags, line


def push_loki(streams):
    request = urllib.request.Request(LOKI_URL, data=json.dumps({"streams": streams}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with HTTP.open(request, timeout=TIMEOUT) as response:
        if not 200 <= response.status < 300:
            raise RuntimeError("Loki 写入失败")


class LogForwarder:
    def __init__(self, panel, state_dir=STATE_DIR):
        self.panel = panel
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        identity = hashlib.sha256((panel["name"] + "\n" + panel["url"]).encode()).hexdigest()
        self.db = sqlite3.connect(str(Path(state_dir) / (identity + ".sqlite")))
        self.db.execute("CREATE TABLE IF NOT EXISTS seen (digest TEXT PRIMARY KEY, occurrences INTEGER, touched REAL)")

    def forward(self, entries, now=None):
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise ValueError("访问日志响应必须是数组")
        now = time.time() if now is None else now
        counts, records = Counter(), {}
        invalid = 0
        for entry in entries:
            try:
                digest, ns, tags, line = normalize_log(entry, self.panel["name"])
                if not (now - self.panel.get("max_log_age", 3600)) * 1e9 <= ns <= (now + 300) * 1e9:
                    continue  # avoid Loki rejecting an entire batch of expired records
                counts[digest] += 1
                records[digest] = (ns, tags, line)
            except (ValueError, TypeError, AttributeError):
                invalid += 1
        had_previous = self.db.execute("SELECT 1 FROM seen LIMIT 1").fetchone() is not None
        streams, updates, overlap = {}, [], False
        for digest, count in counts.items():
            row = self.db.execute("SELECT occurrences FROM seen WHERE digest=?", (digest,)).fetchone()
            previous = row[0] if row else 0
            overlap |= previous > 0
            ns, tags, line = records[digest]
            key = tuple(sorted(tags.items()))
            if count > previous:
                stream = streams.setdefault(key, {"stream": tags, "values": []})
                stream["values"].extend([[str(ns + ordinal), line] for ordinal in range(previous, count)])
            updates.append((digest, max(previous, count), now))
        for stream in streams.values():
            stream["values"].sort(key=lambda value: int(value[0]))
        if streams:
            push_loki(list(streams.values()))
        # Only checkpoint after the receiver acknowledges the batch. Retry uses
        # the same timestamps and lines, including distinct duplicate occurrences.
        with self.db:
            self.db.executemany("INSERT OR REPLACE INTO seen VALUES (?, ?, ?)", updates)
            self.db.execute("DELETE FROM seen WHERE touched < ?", (now - 7 * 86400,))
        full = len(entries) >= self.panel.get("log_count", 1000)
        gap = full and had_previous and not overlap and bool(counts)
        return {"window_full": int(full), "window_gap": int(gap), "invalid_records": invalid}

    def close(self):
        self.db.close()


def error_kind(error):
    if isinstance(error, urllib.error.HTTPError):
        return f"HTTP {error.code}"
    return type(error).__name__  # never print Token, panel URLs or response bodies


class PanelWorker:
    def __init__(self, panel):
        self.panel = panel
        self.lock = threading.Lock()
        self.metrics = [metric("xui_exporter_up", 0, server=panel["name"])]
        self.log_status = {"enabled": int(panel.get("logs", False)), "up": 0,
                           "last_success_timestamp_seconds": 0, "window_full": 0,
                           "window_gap": 0, "invalid_records": 0}

    def poll(self, forwarder=None):
        panel = self.panel
        try:
            token = read_token(panel["token_file"])
            inbounds = request_json(panel["url"], token=token)
            clients = request_json(endpoint(panel["url"], "/clients/list"), token=token)
            online = request_json(endpoint(panel["url"], "/clients/onlines"), "POST", token=token)
            data = render_metrics(panel["name"], inbounds or [], online or [], clients or [])
        except ERRORS as error:
            data = [metric("xui_exporter_up", 0, server=panel["name"])]
            print(f'{panel["name"]}: 流量采集失败 ({error_kind(error)})', file=sys.stderr, flush=True)
        with self.lock:
            self.metrics = data  # discard stale client series on failure
        if not panel.get("logs", False):
            return
        try:
            if forwarder is None:
                raise RuntimeError("日志状态存储未就绪")
            token = read_token(panel["token_file"])
            entries = request_json(endpoint(panel["url"], f'/server/xraylogs/{panel.get("log_count", 1000)}'),
                                   "POST", token=token, form={"showDirect": "true", "showBlocked": "true", "showProxy": "true"})
            stats = forwarder.forward(entries)
            with self.lock:
                self.log_status.update(stats, up=1, last_success_timestamp_seconds=time.time())
            if stats["window_gap"]:
                print(f'{panel["name"]}: 日志窗口已满且与上次没有重叠，可能漏采；增加窗口或缩短间隔', file=sys.stderr, flush=True)
        except ERRORS as error:
            with self.lock:
                self.log_status["up"] = 0
            print(f'{panel["name"]}: 日志采集或写入失败 ({error_kind(error)})', file=sys.stderr, flush=True)

    def run(self):
        forwarder = None
        try:
            while True:
                if self.panel.get("logs", False) and forwarder is None:
                    try:
                        forwarder = LogForwarder(self.panel)
                    except ERRORS as error:
                        print(f'{self.panel["name"]}: 日志状态存储失败 ({error_kind(error)})', file=sys.stderr, flush=True)
                start = time.monotonic()
                self.poll(forwarder)
                time.sleep(max(1, self.panel.get("interval", 30) - (time.monotonic() - start)))
        finally:
            if forwarder:
                forwarder.close()

    def snapshot(self):
        with self.lock:
            return self.metrics + [metric("xui_access_log_" + key, value, server=self.panel["name"])
                                   for key, value in self.log_status.items()]


WORKERS = []


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/metrics", "/metrics?"):
            self.send_error(404)
            return
        try:
            content = ("\n".join(headers() + [line for worker in WORKERS for line in worker.snapshot()]) + "\n") if PANELS_FILE else collect()
            body = content.encode("utf-8")
        except ERRORS as error:
            print(f"xui exporter error: {error_kind(error)}", file=sys.stderr, flush=True)
            body = ("\n".join(headers() + [metric("xui_exporter_up", 0, server=SERVER_NAME)]) + "\n").encode()
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def log_message(self, *_args):
        pass


def panels_cli(argv):
    parser = argparse.ArgumentParser(description="管理中心端 3x-ui 面板；Token 从标准输入读取")
    parser.add_argument("operation", choices=("list", "set", "delete", "migrate"))
    parser.add_argument("--name")
    parser.add_argument("--url")
    parser.add_argument("--logs", choices=("true", "false"), default="true")
    parser.add_argument("--interval", type=int, default=30)
    parser.add_argument("--log-count", type=int, default=1000)
    args = parser.parse_args(argv)
    if not PANELS_FILE:
        raise ValueError("缺少 XUI_PANELS_FILE")
    panels = load_config(PANELS_FILE)
    if args.operation == "list":
        for panel in panels:
            print(f'{panel["name"]}\t{panel["url"]}\t日志={panel.get("logs", False)}\t间隔={panel.get("interval", 30)}s\t窗口={panel.get("log_count", 1000)}')
        return
    if not args.name or not NAME_RE.fullmatch(args.name):
        raise ValueError("请指定有效的面板名称")
    previous = next((p for p in panels if p["name"] == args.name), None)
    remaining = [p for p in panels if p["name"] != args.name]
    if args.operation in ("set", "migrate"):
        token = sys.stdin.read().strip()
        if args.operation == "migrate" and previous:
            return  # preserve an already configured panel on repeated upgrades
        token_path = str(Path(PANELS_FILE).parent / f"xui-{args.name}.token")
        panel = validate_panel({"name": args.name, "url": args.url or "", "token_file": f"/run/secrets/xui-{args.name}.token",
                                "logs": args.logs == "true", "interval": args.interval, "log_count": args.log_count})
        if token:
            if "\n" in token or "\r" in token:
                raise ValueError("Token 不能包含换行符")
            atomic_write(token_path, token + "\n")
        elif not previous:
            raise ValueError("新面板必须提供 Token")
        else:
            read_token(token_path)
        remaining.append(panel)
    atomic_write(PANELS_FILE, json.dumps(remaining, ensure_ascii=False, indent=2) + "\n")
    if args.operation == "delete" and previous:
        (Path(PANELS_FILE).parent / f"xui-{args.name}.token").unlink(missing_ok=True)


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "panels":
        panels_cli(sys.argv[2:])
        return
    if PANELS_FILE:
        panels = load_config(PANELS_FILE)
        for panel in panels:
            worker = PanelWorker(panel)
            WORKERS.append(worker)
            threading.Thread(target=worker.run, daemon=True, name=panel["name"]).start()
    elif not API_URL:
        raise ValueError("缺少 XUI_API_URL 或 XUI_PANELS_FILE")
    print("xui exporter started", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9105), MetricsHandler).serve_forever()


if __name__ == "__main__":
    try:
        main()
    except ERRORS as error:
        print(f"启动/配置失败: {error_kind(error)}", file=sys.stderr)
        raise SystemExit(1)
