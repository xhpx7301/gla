#!/usr/bin/env python3
"""Prometheus exporter for the read-only 3x-ui Panel API traffic endpoints."""

import json
import os
import sys
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


API_URL = os.environ.get("XUI_API_URL", "").rstrip("/")
TOKEN_FILE = os.environ.get("XUI_API_TOKEN_FILE", "/run/secrets/xui_api_token")
TIMEOUT = float(os.environ.get("XUI_API_TIMEOUT", "10"))
SERVER_NAME = os.environ.get("SERVER_NAME", "").strip() or "unknown"


def metric_escape(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def labels(**values):
    return ",".join(f'{key}="{metric_escape(value)}"' for key, value in values.items())


def read_token():
    with open(TOKEN_FILE, "r", encoding="utf-8") as token_file:
        token = token_file.read().strip()
    if not token:
        raise RuntimeError("3x-ui API Token 文件为空")
    return token


def request_json(url, method="GET"):
    request = urllib.request.Request(
        url,
        data=b"" if method == "POST" else None,
        method=method,
        headers={
            "Authorization": f"Bearer {read_token()}",
            "Accept": "application/json",
            "User-Agent": "gla-xui-exporter/1.0",
        },
    )
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not payload.get("success"):
        raise RuntimeError(payload.get("msg") or "3x-ui API 返回 success=false")
    return payload.get("obj") or []


def online_url():
    marker = "/inbounds/list"
    if marker not in API_URL:
        raise RuntimeError("XUI_API_URL 必须指向 /panel/api/inbounds/list")
    return API_URL.replace(marker, "/clients/onlines", 1)


def collect():
    inbounds = request_json(API_URL)
    online_emails = set(request_json(online_url(), "POST"))
    lines = [
        "# HELP xui_exporter_up 3x-ui Panel API 是否可访问。",
        "# TYPE xui_exporter_up gauge",
        f"xui_exporter_up{{{labels(server=SERVER_NAME)}}} 1",
        "# HELP xui_client_traffic_bytes_total 3x-ui 客户端累计流量字节数。",
        "# TYPE xui_client_traffic_bytes_total counter",
        "# HELP xui_inbound_traffic_bytes_total 3x-ui 入站累计流量字节数。",
        "# TYPE xui_inbound_traffic_bytes_total counter",
        "# HELP xui_client_online 3x-ui API 报告的当前在线客户端。",
        "# TYPE xui_client_online gauge",
        "# HELP xui_client_last_online_timestamp_seconds 客户端最后在线时间。",
        "# TYPE xui_client_last_online_timestamp_seconds gauge",
    ]

    for inbound in inbounds:
        inbound_labels = {
            "server": SERVER_NAME,
            "inbound_id": inbound.get("id", 0),
            "inbound": inbound.get("remark") or inbound.get("tag") or "unnamed",
            "port": inbound.get("port", 0),
            "protocol": inbound.get("protocol") or "unknown",
        }
        for direction, key in (("uplink", "up"), ("downlink", "down")):
            lines.append(
                f"xui_inbound_traffic_bytes_total{{{labels(direction=direction, **inbound_labels)}}} {int(inbound.get(key) or 0)}"
            )

        for client in inbound.get("clientStats") or []:
            email = client.get("email") or "unknown"
            client_labels = {"email": email, **inbound_labels}
            for direction, key in (("uplink", "up"), ("downlink", "down")):
                lines.append(
                    f"xui_client_traffic_bytes_total{{{labels(direction=direction, **client_labels)}}} {int(client.get(key) or 0)}"
                )
            lines.append(
                f"xui_client_online{{{labels(**client_labels)}}} {1 if email in online_emails else 0}"
            )
            last_online = int(client.get("lastOnline") or 0) / 1000
            lines.append(
                f"xui_client_last_online_timestamp_seconds{{{labels(**client_labels)}}} {last_online:.3f}"
            )

    return "\n".join(lines) + "\n"


class MetricsHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path not in ("/metrics", "/metrics?"):
            self.send_error(404)
            return
        try:
            body = collect().encode("utf-8")
            status = 200
        except (OSError, RuntimeError, ValueError, urllib.error.URLError, json.JSONDecodeError) as error:
            print(f"xui exporter error: {error}", file=sys.stderr, flush=True)
            body = (
                "# HELP xui_exporter_up 3x-ui Panel API 是否可访问。\n"
                "# TYPE xui_exporter_up gauge\n"
                f"xui_exporter_up{{{labels(server=SERVER_NAME)}}} 0\n"
            ).encode("utf-8")
            status = 200
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            return

    def log_message(self, *_args):
        return


if __name__ == "__main__":
    if not API_URL:
        raise SystemExit("缺少 XUI_API_URL")
    print("xui exporter started", flush=True)
    ThreadingHTTPServer(("0.0.0.0", 9105), MetricsHandler).serve_forever()
