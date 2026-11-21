#!/usr/bin/env python3
"""3x-ui 3.3/3.5 API metrics and persistent Xray access-log forwarding.

Legacy XUI_API_URL deployments still expose metrics on demand. A panels file
enables background polling so slow remote panels cannot block Prometheus.
"""

import argparse
import errno
import hmac
from collections import Counter
from datetime import datetime, timedelta, timezone
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import signal
import socket
import sqlite3
import ssl
import sys
import tempfile
import threading
import time
import urllib.parse
import urllib.error
import urllib.request
from urllib.parse import quote
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import maxminddb
except ImportError:  # Local tests and utility commands do not need MMDB support.
    maxminddb = None


API_URL = os.environ.get("XUI_API_URL", "").rstrip("/")
TOKEN_FILE = os.environ.get("XUI_API_TOKEN_FILE", "/run/secrets/xui_api_token")
PANELS_FILE = os.environ.get("XUI_PANELS_FILE", "")
STATE_DIR = os.environ.get("XUI_STATE_DIR", "/var/lib/xui")
LOKI_URL = os.environ.get("XUI_LOKI_URL", "http://alloy:3500/loki/api/v1/push")
TELEGRAM_CONFIG_FILE = os.environ.get("XUI_TELEGRAM_CONFIG_FILE", "/run/secrets/telegram-config.json")
SSH_TELEGRAM_CONFIG_FILE = os.environ.get("XUI_SSH_TELEGRAM_CONFIG_FILE", "/run/secrets/ssh-telegram-config.json")
SSH_LOGIN_CONFIG_FILE = os.environ.get("XUI_SSH_LOGIN_CONFIG_FILE", "/run/secrets/ssh-login-config.json")
WEB_ADMIN_CONFIG_FILE = os.environ.get("XUI_WEB_ADMIN_CONFIG_FILE", "/run/secrets/web-admin.json")
ADMIN_HTML_FILE = os.environ.get("XUI_ADMIN_HTML_FILE", str(Path(__file__).with_name("admin.html")))
DEFAULT_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS = 3600
PREVIOUS_DEFAULT_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS = (600, 1800)
MAX_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS = 86400
IP_RECORD_MAX_AGE_SECONDS = 86400
IP_RECORD_MAX_FUTURE_SKEW_SECONDS = 300
TELEGRAM_ONLINE_CONFIRMATION_SAMPLES = 20
MAX_ONLINE_CONFIRMATION_SAMPLES = 60
TELEGRAM_ONLINE_FAST_CONFIRMATION_SAMPLES = 3
TELEGRAM_ONLINE_FAST_CONFIRMATION_MIN_BYTES = 100 * 1024
LOCAL_SOURCE_NETWORKS = tuple(ipaddress.ip_network(cidr) for cidr in (
    "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "100.64.0.0/10", "fc00::/7"))
PROBE_TARGET_HOSTS = frozenset({
    "www.gstatic.com", "connectivitycheck.gstatic.com", "clients3.google.com",
    "cp.cloudflare.com", "detectportal.firefox.com", "captive.apple.com",
    "www.msftconnecttest.com", "www.msftncsi.com",
})
CLIENT_MUTE_SECONDS = 6 * 3600
TIMEOUT = float(os.environ.get("XUI_API_TIMEOUT", "10"))
SERVER_NAME = os.environ.get("SERVER_NAME", "").strip() or "unknown"
GEOIP_CITY_DB = os.environ.get("GEOIP_CITY_DB", "/var/lib/gla/geoip/GeoLite2-City.mmdb")
GEOIP_ASN_DB = os.environ.get("GEOIP_ASN_DB", "/var/lib/gla/geoip/GeoLite2-ASN.mmdb")
_MMDB_READERS = {}
_MMDB_LOCK = threading.Lock()
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


def validate_notification_cooldown(value):
    if (type(value) is not int or value < 0
            or value > MAX_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS):
        raise ValueError("Telegram 通知冷却必须是 0 到 86400 秒的整数")
    return value


def configured_notification_cooldown(value):
    if value in PREVIOUS_DEFAULT_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS:
        return DEFAULT_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS
    return validate_notification_cooldown(value)


def validate_online_confirmation_samples(value):
    if (type(value) is not int or value < 1
            or value > MAX_ONLINE_CONFIRMATION_SAMPLES):
        raise ValueError("上线确认采样次数必须是 1 到 60 的整数")
    return value


def telegram_config(path=None):
    config_path = Path(path or TELEGRAM_CONFIG_FILE)
    if not config_path.exists():
        return None
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Telegram 配置必须是 JSON 对象")
    user_ids = config.get("allowed_user_ids", [])
    chat_ids = config.get("allowed_chat_ids", [])
    if (not isinstance(config, dict) or not isinstance(user_ids, list) or not isinstance(chat_ids, list)
            or (not user_ids and not chat_ids)
            or any(type(value) is not int for value in [*user_ids, *chat_ids])):
        raise ValueError("Telegram 配置无效：必须配置数字用户 ID 或 Chat ID")
    token_file = config.get("token_file", "")
    if not isinstance(token_file, str) or not token_file.startswith("/run/secrets/") or "/" in token_file[len("/run/secrets/"):] :
        raise ValueError("Telegram Token 文件必须位于 /run/secrets/ 目录")
    cooldown = configured_notification_cooldown(
        config.get("notification_cooldown_seconds", DEFAULT_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS))
    confirmation_samples = validate_online_confirmation_samples(
        config.get("online_confirmation_samples", TELEGRAM_ONLINE_CONFIRMATION_SAMPLES))
    cross_panel = bool(config.get("cross_panel_cooldown", False))
    return {**config, "allowed_user_ids": user_ids, "allowed_chat_ids": chat_ids,
            "notification_cooldown_seconds": cooldown,
            "online_confirmation_samples": confirmation_samples,
            "cross_panel_cooldown": cross_panel,
            "token": read_token(token_file)}


def telegram_request(token, method, payload=None, timeout=35):
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = urllib.parse.urlencode(payload).encode() if payload is not None else None
    request = urllib.request.Request(url, data=data, method="POST" if data is not None else "GET")
    with HTTP.open(request, timeout=timeout) as response:
        result = json.load(response)
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("Telegram API 返回无效响应")
    return result.get("result")


def parse_metric_line(line):
    match = re.fullmatch(r'([a-zA-Z_:][a-zA-Z0-9_:]*)\{(.*)\}\s+([-+0-9.eE]+)', line)
    if not match:
        return None
    tags = {}
    for item in re.finditer(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:\\.|[^"\\])*)"', match[2]):
        tags[item[1]] = item[2].replace('\\n', '\n').replace('\\"', '"').replace('\\\\', '\\')
    return match[1], tags, float(match[3])


def format_bytes(value):
    value = max(0, int(value))
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.2f} {unit}"
        value /= 1024


def normalize_notify_time(value):
    """Accept 8:00 and 08:00, storing a canonical two-digit-hour value."""
    match = re.fullmatch(r"(\d{1,2}):([0-5]\d)", str(value).strip())
    if not match or int(match[1]) > 23:
        return None
    return f"{int(match[1]):02d}:{match[2]}"


class TelegramBot:
    BUTTON_COMMANDS = {
        "菜单": "/menu", "☰ 菜单": "/menu", "M": "/menu",
        "📡 状态": "/status", "👥 分组": "/groups", "🟢 在线客户端": "/online",
        "📈 累计流量": "/traffic", "🔔 添加订阅": "/watch",
        "🔕 取消订阅": "/unwatch", "📋 我的订阅": "/watches", "💡 帮助": "/help",
        "🔔 上线通知": "/online_notify", "📊 流量通知": "/traffic_notify",
        "📖 命令说明": "/commands",
    }
    COMMANDS = [
        {"command": "start", "description": "显示主菜单"},
        {"command": "help", "description": "机器人帮助"},
        {"command": "commands", "description": "查看完整命令及用法"},
        {"command": "status", "description": "检查机器人状态"},
        {"command": "id", "description": "显示您的 Telegram ID"},
        {"command": "groups", "description": "查看客户端分组"},
        {"command": "online", "description": "查看在线客户端"},
        {"command": "traffic", "description": "查看分组累计流量"},
        {"command": "traffic_panels", "description": "查看分组各面板累计流量"},
        {"command": "online_notify", "description": "管理上线通知"},
        {"command": "watch", "description": "订阅分组上线通知"},
        {"command": "unwatch", "description": "取消分组上线通知"},
        {"command": "watches", "description": "查看已订阅分组"},
        {"command": "traffic_notify", "description": "配置定时流量通知"},
        {"command": "menu", "description": "菜单"},
    ]

    def __init__(self, config, workers, state_dir):
        self.config = config
        self.workers = workers
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(state_dir) / "telegram.sqlite"), timeout=10, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS state (server TEXT, email TEXT, group_name TEXT, online INTEGER, PRIMARY KEY(server,email))")
        self.db.execute("CREATE TABLE IF NOT EXISTS subscriptions (chat_id INTEGER, group_name TEXT, PRIMARY KEY(chat_id,group_name))")
        self.db.execute("CREATE TABLE IF NOT EXISTS online_notify_settings (chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 1)")
        self.db.execute("CREATE TABLE IF NOT EXISTS notification_cooldowns (chat_id INTEGER, server TEXT, email TEXT, notified_at REAL NOT NULL, PRIMARY KEY(chat_id,server,email))")
        self.db.execute("CREATE TABLE IF NOT EXISTS client_mutes (chat_id INTEGER, server TEXT, email TEXT, until_at REAL NOT NULL, PRIMARY KEY(chat_id,server,email))")
        self.db.execute("CREATE TABLE IF NOT EXISTS online_confirmations (server TEXT, email TEXT, last_sample_at REAL NOT NULL, consecutive_online INTEGER NOT NULL, start_traffic INTEGER, first_seen_at REAL, PRIMARY KEY(server,email))")
        confirmation_columns = {row[1] for row in self.db.execute("PRAGMA table_info(online_confirmations)")}
        if "start_traffic" not in confirmation_columns:
            self.db.execute("ALTER TABLE online_confirmations ADD COLUMN start_traffic INTEGER")
        if "first_seen_at" not in confirmation_columns:
            self.db.execute("ALTER TABLE online_confirmations ADD COLUMN first_seen_at REAL")
        self.db.execute("CREATE TABLE IF NOT EXISTS traffic_notify_settings (chat_id INTEGER PRIMARY KEY, enabled INTEGER NOT NULL DEFAULT 0, notify_time TEXT NOT NULL DEFAULT '20:00')")
        self.db.execute("CREATE TABLE IF NOT EXISTS traffic_notify_groups (chat_id INTEGER, group_name TEXT, PRIMARY KEY(chat_id,group_name))")
        self.db.execute("CREATE TABLE IF NOT EXISTS traffic_notify_baselines (chat_id INTEGER, group_name TEXT, server TEXT, up INTEGER NOT NULL, down INTEGER NOT NULL, PRIMARY KEY(chat_id,group_name,server))")
        self.db.execute("CREATE TABLE IF NOT EXISTS traffic_notify_runs (chat_id INTEGER PRIMARY KEY, sent_date TEXT NOT NULL)")
        self.db.execute("CREATE TABLE IF NOT EXISTS traffic_notify_options (chat_id INTEGER PRIMARY KEY, show_panels INTEGER NOT NULL DEFAULT 0)")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        self.offset = 0
        self.pending_groups = {}
        self.pending_online_groups = {}
        self.pending_traffic_groups = {}
        self.pending_traffic_times = set()
        self.pending_traffic_views = {}
        self.pending_notification_actions = {}
        self.notification_cooldown_seconds = validate_notification_cooldown(
            config.get("notification_cooldown_seconds", DEFAULT_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS))
        self.online_confirmation_samples = validate_online_confirmation_samples(
            config.get("online_confirmation_samples", TELEGRAM_ONLINE_CONFIRMATION_SAMPLES))
        self.cross_panel_cooldown = bool(config.get("cross_panel_cooldown", False))

    def snapshot(self):
        series = {}
        servers_up = set()
        server_samples = {}
        for worker in self.workers:
            for line in worker.snapshot():
                parsed = parse_metric_line(line)
                if not parsed:
                    continue
                name, tags, value = parsed
                server = tags.get("server", "")
                if name == "xui_exporter_up" and value == 1:
                    servers_up.add(server)
                elif name == "xui_metrics_last_success_timestamp_seconds":
                    server_samples[server] = value
                if name == "xui_client_online":
                    key = (server, tags.get("email", ""))
                    series.setdefault(key, {"server": server, "email": key[1], "group": tags.get("group", "未分组"), "online": 0, "up": 0, "down": 0})
                    series[key]["online"] = int(value)
                elif name == "xui_client_traffic_bytes_total":
                    key = (server, tags.get("email", ""))
                    series.setdefault(key, {"server": server, "email": key[1], "group": tags.get("group", "未分组"), "online": 0, "up": 0, "down": 0})
                    series[key]["up" if tags.get("direction") == "uplink" else "down"] = int(value)
        for item in series.values():
            item["up"] = item["up"] if item["server"] in servers_up else 0
            item["down"] = item["down"] if item["server"] in servers_up else 0
            item["sample_at"] = server_samples.get(item["server"])
        return series, servers_up

    @staticmethod
    def main_keyboard():
        return {"inline_keyboard": [
            [{"text": "📡 状态", "callback_data": "menu:status"},
             {"text": "👥 分组", "callback_data": "menu:groups"}],
            [{"text": "🟢 在线客户端", "callback_data": "menu:online"},
             {"text": "📈 累计流量", "callback_data": "menu:traffic"}],
            [{"text": "🔔 上线通知", "callback_data": "menu:online_notify"}],
            [{"text": "📊 流量通知", "callback_data": "menu:traffic_notify"}],
            [{"text": "📋 我的订阅", "callback_data": "menu:watches"},
             {"text": "💡 帮助", "callback_data": "menu:help"}],
            [{"text": "📖 命令说明", "callback_data": "menu:commands"}],
        ]}

    def send(self, chat_id, text, reply_markup=None, entities=None):
        # Reports can grow beyond Telegram's message limit. Split at line
        # boundaries and count UTF-16 units so emoji also fit safely.
        chunks, current, units = [], "", 0
        for line in text.splitlines(keepends=True):
            for char in line:
                size = len(char.encode("utf-16-le")) // 2
                if units + size > 3500:
                    boundary = current.rfind("\n") + 1
                    if boundary:
                        chunks.append(current[:boundary])
                        current = current[boundary:]
                        units = len(current.encode("utf-16-le")) // 2
                    else:
                        chunks.append(current)
                        current, units = "", 0
                current += char
                units += size
        if current:
            chunks.append(current)
        chunk_offset = 0
        for index, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk}
            chunk_units = len(chunk.encode("utf-16-le")) // 2
            chunk_entities = []
            for entity in entities or []:
                start = max(entity["offset"], chunk_offset)
                end = min(entity["offset"] + entity["length"], chunk_offset + chunk_units)
                if start < end:
                    chunk_entities.append({**entity, "offset": start - chunk_offset, "length": end - start})
            if chunk_entities:
                payload["entities"] = json.dumps(chunk_entities, ensure_ascii=False, separators=(",", ":"))
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False, separators=(",", ":"))
            telegram_request(self.config["token"], "sendMessage", payload)
            chunk_offset += chunk_units

    @staticmethod
    def report_time():
        return datetime.fromtimestamp(time.time(), timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S") + "（UTC+8）"

    @staticmethod
    def traffic_details(up, down):
        return (f"⬆️ 上行：{format_bytes(up)}\n"
                f"⬇️ 下行：{format_bytes(down)}\n"
                f"📊 总计：{format_bytes(up + down)}")

    def report_footer(self, servers_up):
        unavailable = [worker.panel["name"] for worker in self.workers
                       if worker.panel["name"] not in servers_up]
        note = ""
        if unavailable:
            note = "\n⚠️ 以下面板暂不可用，未计入本次结果：" + "、".join(unavailable)
        return note + "\n\n🕒 查询时间：" + self.report_time()

    def register_commands(self):
        # Keep Telegram's native bottom-left button on the command menu.  The
        # native button text is controlled by the Telegram client; the bot
        # exposes /menu as the localized in-chat menu entry.
        try:
            telegram_request(self.config["token"], "setChatMenuButton", {
                "menu_button": json.dumps({"type": "commands"}, separators=(",", ":"))
            })
        except ERRORS as error:
            print(f"Telegram 原生菜单按钮设置失败 ({error_kind(error)})", file=sys.stderr, flush=True)
        telegram_request(self.config["token"], "setMyCommands", {
            "commands": json.dumps(self.COMMANDS, ensure_ascii=False, separators=(",", ":"))
        })

    def traffic_notify_keyboard(self, chat_id):
        row = self.db.execute("SELECT enabled,notify_time FROM traffic_notify_settings WHERE chat_id=?", (chat_id,)).fetchone()
        enabled, notify_time = row if row else (0, "20:00")
        show_panels = self.traffic_notify_show_panels(chat_id)
        groups = [item[0] for item in self.db.execute(
            "SELECT group_name FROM traffic_notify_groups WHERE chat_id=? ORDER BY group_name", (chat_id,))]
        return (f"📊 定时流量通知：{'开启' if enabled else '关闭'}\n"
                f"🕒 通知时间：每天 {notify_time}（UTC+8）\n"
                f"👥 通知分组：{'、'.join(groups) if groups else '未设置'}\n"
                f"🖥 分组面板明细：{'开启' if show_panels else '关闭'}", {
                    "inline_keyboard": [
                        [{"text": "🔔 开启通知" if not enabled else "🔕 关闭通知",
                          "callback_data": f"trafficnotify:toggle:{0 if enabled else 1}"}],
                        [{"text": f"🕒 修改时间（{notify_time}）", "callback_data": "trafficnotify:time"}],
                        [{"text": "📋 订阅分组", "callback_data": "trafficnotify:groups"}],
                        [{"text": "➕ 新增分组", "callback_data": "trafficnotify:add"},
                         {"text": "➖ 移除分组", "callback_data": "trafficnotify:remove"}],
                        [{"text": "🧪 发送测试通知", "callback_data": "trafficnotify:test"}],
                        [{"text": "🖥 关闭分组面板明细" if show_panels else "🖥 开启分组面板明细",
                          "callback_data": f"trafficnotify:panels:{0 if show_panels else 1}"}],
                        [{"text": "↩️ 返回主菜单", "callback_data": "menu:home"}],
                    ]})

    def send_traffic_notify_settings(self, chat_id, prefix=""):
        text, keyboard = self.traffic_notify_keyboard(chat_id)
        self.send(chat_id, prefix + text, keyboard)

    def set_traffic_notify_time(self, chat_id, notify_time):
        """Rearm a changed schedule while keeping the last successful totals."""
        with self.db:
            previous = self.db.execute(
                "SELECT notify_time FROM traffic_notify_settings WHERE chat_id=?", (chat_id,)).fetchone()
            self.db.execute("INSERT INTO traffic_notify_settings(chat_id,enabled,notify_time) VALUES (?,0,?) "
                            "ON CONFLICT(chat_id) DO UPDATE SET notify_time=excluded.notify_time",
                            (chat_id, notify_time))
            if previous is None or previous[0] != notify_time:
                self.db.execute("DELETE FROM traffic_notify_runs WHERE chat_id=?", (chat_id,))

    def traffic_notify_show_panels(self, chat_id):
        row = self.db.execute("SELECT show_panels FROM traffic_notify_options WHERE chat_id=?",
                              (chat_id,)).fetchone()
        return bool(row[0]) if row else False

    def set_traffic_notify_panels(self, chat_id, enabled):
        with self.db:
            self.db.execute("INSERT INTO traffic_notify_options VALUES (?,?) "
                            "ON CONFLICT(chat_id) DO UPDATE SET show_panels=excluded.show_panels",
                            (chat_id, int(enabled)))

    @staticmethod
    def traffic_totals(series, servers_up):
        totals = {}
        for item in series.values():
            if item["server"] in servers_up:
                total = totals.setdefault((item["group"], item["server"]), [0, 0])
                total[0] += item["up"]
                total[1] += item["down"]
        return totals

    @staticmethod
    def traffic_delta_text(delta_up, delta_down, tracked, count):
        if not tracked:
            return "🆕 较上次新增：首次记录"
        text = (f"➕ 较上次新增：{format_bytes(delta_up + delta_down)}"
                f"（⬆️ {format_bytes(delta_up)} / ⬇️ {format_bytes(delta_down)}）")
        if tracked < count:
            text += "\nℹ️ 部分流量首次记录，新增仅统计已有记录的流量。"
        return text

    def traffic_notification_rows(self, chat_id, groups, totals):
        """Keep per-group, per-panel baselines while presenting compact totals."""
        group_values = {group: [0, 0, 0, 0, 0, 0] for group in groups}
        panel_values = {}
        current_values = []
        for (group, server), (up, down) in totals.items():
            if group not in group_values:
                continue
            previous = self.db.execute(
                "SELECT up,down FROM traffic_notify_baselines WHERE chat_id=? AND group_name=? AND server=?",
                (chat_id, group, server)).fetchone()
            delta_up = max(0, up - previous[0]) if previous else 0
            delta_down = max(0, down - previous[1]) if previous else 0
            values = (up, down, delta_up, delta_down, int(previous is not None), 1)
            for index, value in enumerate(values):
                group_values[group][index] += value
            panel_values.setdefault(group, {})[server] = values
            current_values.append((group, server, up, down))
        rows = []
        show_panels = self.traffic_notify_show_panels(chat_id)
        for group, (up, down, delta_up, delta_down, tracked, count) in sorted(
                group_values.items(), key=lambda entry: (-(entry[1][0] + entry[1][1]), entry[0])):
            if not count:
                rows.append(f"👥 分组：{group}\n⚠️ 当前没有可用面板的流量数据")
            else:
                rows.append(f"👥 分组：{group}\n{self.traffic_details(up, down)}\n"
                            + self.traffic_delta_text(delta_up, delta_down, tracked, count))
                if show_panels:
                    for server, (panel_up, panel_down, panel_delta_up, panel_delta_down, panel_tracked, _) in sorted(
                            panel_values[group].items(), key=lambda entry: (-(entry[1][0] + entry[1][1]), entry[0])):
                        rows.append(f"🖥 面板：{server}\n{self.traffic_details(panel_up, panel_down)}\n"
                                    + self.traffic_delta_text(panel_delta_up, panel_delta_down, panel_tracked, 1))
        return rows, current_values

    def send_traffic_query(self, chat_id, argument="", by_panel=False):
        series, servers_up = self.snapshot()
        totals = {}
        panel_values = {}
        for (group, server), (up, down) in self.traffic_totals(series, servers_up).items():
            if argument and group != argument:
                continue
            total = totals.setdefault(group, [0, 0])
            total[0] += up
            total[1] += down
            panel_values.setdefault(group, {})[server] = (up, down)
        rows = []
        for group, (up, down) in sorted(totals.items(), key=lambda entry: (-sum(entry[1]), entry[0])):
            if by_panel:
                panels = [f"🖥 面板：{server}\n{self.traffic_details(panel_up, panel_down)}"
                          for server, (panel_up, panel_down) in sorted(
                              panel_values[group].items(), key=lambda entry: (-sum(entry[1]), entry[0]))]
                rows.append(f"👥 分组：{group}\n\n" + "\n\n".join(panels))
            else:
                rows.append(f"👥 分组：{group}\n{self.traffic_details(up, down)}")
        title = "🖥 分组面板累计流量" if by_panel else "📈 分组累计流量"
        scope = f"\n👥 筛选分组：{argument}" if argument else ""
        note = ("ℹ️ 按分组列出各面板的客户端累计流量，分组及组内面板按累计总量排序。" if by_panel else
                "ℹ️ 同名分组跨面板汇总，按累计总量排序。")
        reply = (title + scope + "\n\n" + ("\n\n".join(rows) if rows else "📭 没有匹配的流量数据")
                 + "\n\n" + note + self.report_footer(servers_up))
        token = hashlib.sha256(argument.encode()).hexdigest()[:16] if argument else "all"
        if argument:
            self.pending_traffic_views[(chat_id, token)] = argument
            while len(self.pending_traffic_views) > 256:
                self.pending_traffic_views.pop(next(iter(self.pending_traffic_views)))
        target = "groups" if by_panel else "panels"
        keyboard = {"inline_keyboard": [
            [{"text": "👥 查看分组累计流量" if by_panel else "🖥 查看分组面板明细",
              "callback_data": f"trafficview:{target}:{token}"}],
            [{"text": "↩️ 返回主菜单", "callback_data": "menu:home"}],
        ]}
        self.send(chat_id, reply, keyboard)

    def send_traffic_notify_test(self, chat_id):
        """Send current traffic without changing the scheduled notification state."""
        now = time.time()
        local_now = datetime.fromtimestamp(now, timezone(timedelta(hours=8)))
        series, servers_up = self.snapshot()
        groups = [row[0] for row in self.db.execute(
            "SELECT group_name FROM traffic_notify_groups WHERE chat_id=? ORDER BY group_name", (chat_id,))]
        if not groups:
            self.send(chat_id, "🧪 流量通知测试\n\n尚未设置通知分组，请先新增分组。")
            return
        if not servers_up:
            self.send(chat_id, "🧪 流量通知测试\n\n⚠️ 当前没有可用面板，暂时无法获取流量数据。")
            return
        rows, _ = self.traffic_notification_rows(chat_id, groups, self.traffic_totals(series, servers_up))
        text = (f"🧪 流量通知测试\n🕒 当前时间：{local_now.strftime('%Y-%m-%d %H:%M')}（UTC+8）\n\n"
                + "\n\n".join(rows) + "\n\nℹ️ 测试不会更新流量对比记录或当天发送状态。"
                + self.report_footer(servers_up))
        self.send(chat_id, text)

    def online_notify_keyboard(self, chat_id):
        groups = [item[0] for item in self.db.execute(
            "SELECT group_name FROM subscriptions WHERE chat_id=? ORDER BY group_name", (chat_id,))]
        setting = self.db.execute("SELECT enabled FROM online_notify_settings WHERE chat_id=?", (chat_id,)).fetchone()
        enabled = bool(setting[0]) if setting is not None else bool(groups)
        cooldown = self.notification_cooldown_seconds
        if cooldown == 0:
            cooldown_text = "关闭"
        elif cooldown % 60 == 0:
            cooldown_text = f"{cooldown // 60} 分钟"
        else:
            cooldown_text = f"{cooldown} 秒"
        return (f"🔔 上线通知：{'开启' if enabled else '关闭'}\n"
                f"👥 订阅分组：{'、'.join(groups) if groups else '未设置'}\n"
                f"⏱️ 通知冷却：{cooldown_text}", {
                    "inline_keyboard": [
                        [{"text": "🔔 开启通知" if not enabled else "🔕 关闭通知",
                          "callback_data": f"onlinenotify:toggle:{0 if enabled else 1}"}],
                        [{"text": "📋 订阅分组", "callback_data": "onlinenotify:groups"}],
                        [{"text": "➕ 新增分组", "callback_data": "onlinenotify:add"},
                         {"text": "➖ 移除分组", "callback_data": "onlinenotify:remove"}],
                        [{"text": "🧪 发送测试通知", "callback_data": "onlinenotify:test"}],
                        [{"text": "↩️ 返回主菜单", "callback_data": "menu:home"}],
                    ]})

    def send_online_notify_settings(self, chat_id, prefix=""):
        text, keyboard = self.online_notify_keyboard(chat_id)
        self.send(chat_id, prefix + text, keyboard)

    def send_online_notify_groups(self, chat_id):
        groups = [row[0] for row in self.db.execute(
            "SELECT group_name FROM subscriptions WHERE chat_id=? ORDER BY group_name", (chat_id,))]
        text = ("📋 上线通知订阅分组\n\n"
                + ("\n".join(f"🔔 {group}" for group in groups) if groups else "📭 暂无上线通知订阅分组"))
        self.send(chat_id, text, self.online_notify_keyboard(chat_id)[1])

    def send_traffic_notify_groups(self, chat_id):
        groups = [row[0] for row in self.db.execute(
            "SELECT group_name FROM traffic_notify_groups WHERE chat_id=? ORDER BY group_name", (chat_id,))]
        text = ("📋 流量通知订阅分组\n\n"
                + ("\n".join(f"📊 {group}" for group in groups) if groups else "📭 暂无流量通知订阅分组"))
        self.send(chat_id, text, self.traffic_notify_keyboard(chat_id)[1])

    def send_my_subscriptions(self, chat_id):
        online = [row[0] for row in self.db.execute(
            "SELECT group_name FROM subscriptions WHERE chat_id=? ORDER BY group_name", (chat_id,))]
        traffic = [row[0] for row in self.db.execute(
            "SELECT group_name FROM traffic_notify_groups WHERE chat_id=? ORDER BY group_name", (chat_id,))]
        online_setting = self.db.execute(
            "SELECT enabled FROM online_notify_settings WHERE chat_id=?", (chat_id,)).fetchone()
        traffic_setting = self.db.execute(
            "SELECT enabled FROM traffic_notify_settings WHERE chat_id=?", (chat_id,)).fetchone()
        online_enabled = bool(online_setting[0]) if online_setting is not None else bool(online)
        traffic_enabled = bool(traffic_setting[0]) if traffic_setting is not None else False
        text = ("📋 我的订阅\n\n"
                f"🔔 上线通知：{'开启' if online_enabled else '关闭'}\n"
                f"  {'、'.join(online) if online else '暂无分组'}\n\n"
                f"📊 流量通知：{'开启' if traffic_enabled else '关闭'}\n"
                f"  {'、'.join(traffic) if traffic else '暂无分组'}")
        self.send(chat_id, text)

    def inbound_names_for_item(self, item):
        worker = next((worker for worker in self.workers
                       if worker.panel["name"] == item["server"]), None)
        return worker.inbounds_for_client(item["email"]) if worker else []

    def online_client_block(self, item):
        inbounds = self.inbound_names_for_item(item)
        inbound_text = "、".join(inbounds) if inbounds else "未找到配置关联"
        return (f"👤 客户端：{item['email']}\n👥 分组：{item['group']}\n"
                f"🔗 关联入站：{inbound_text}\n🖥 面板：{item['server']}\n"
                f"{self.traffic_details(item['up'], item['down'])}")

    def online_notification_text(self, item, test=False):
        worker = next((worker for worker in self.workers
                       if worker.panel["name"] == item["server"]), None)
        ip_record = recent_ip_record(worker.last_ip_for_client(item["email"])) if worker else None
        if ip_record:
            location, network = ip_attribution(ip_record["ip"])
            network_label = "运营商" if network in {"中国电信", "中国联通", "中国移动"} else "网络组织"
            ip_text = (f"🌐 最近记录 IP：{ip_record['ip']}\n"
                       f"📍 归属地：{location}\n🏢 {network_label}：{network}")
            if ip_record.get("time"):
                ip_text += f"\n🕒 IP 记录时间：{ip_record['time']}"
        elif worker and worker.has_local_source_only(item["email"]):
            ip_text = "🌐 最近记录 IP：暂无24小时内公网记录（来源为本机/内网转发）"
        else:
            ip_text = "🌐 最近记录 IP：暂无24小时内记录"
        inbounds = self.inbound_names_for_item(item)
        if len(inbounds) > 1:
            inbound_text = "\n" + "\n".join(f"  • {inbound}" for inbound in inbounds)
        else:
            inbound_text = inbounds[0] if inbounds else "未找到配置关联"
        detail_lines = []
        if not test and item.get("first_seen_at"):
            first_dt = datetime.fromtimestamp(float(item["first_seen_at"]), timezone(timedelta(hours=8)))
            detail_lines.append(f"⏱ 首次上线：{first_dt.strftime('%Y-%m-%d %H:%M:%S')}（UTC+8）")
        if not test and item.get("delta_bytes"):
            detail_lines.append(f"📊 确认期流量：{format_bytes(item['delta_bytes'])}")
        detail_lines.append(f"🕒 检测时间：{self.report_time()}")
        title = "🧪 上线通知测试" if test else "🟢 客户端上线"
        status = "在线" if item.get("online") else "离线"
        suffix = f"\n\nℹ️ 测试样例，客户端当前状态：{status}。不会记录在线状态或通知冷却。" if test else ""
        return (f"{title}\n\n👥 分组：{item['group']}\n👤 客户端：{item['email']}\n"
                f"🖥 面板：{item['server']}\n🔗 关联入站：{inbound_text}\n{ip_text}\n\n"
                + "\n".join(detail_lines) + suffix)

    def send_online_notification(self, chat_id, notification, reply_markup=None):
        match = re.search(r"^🌐 最近记录 IP：([^\n]+)$", notification, re.MULTILINE)
        try:
            address = str(ipaddress.ip_address(match[1])) if match else None
        except ValueError:
            address = None
        if address is None:
            self.send(chat_id, notification, reply_markup=reply_markup)
            return
        # Telegram entity offsets count UTF-16 units, including emoji before the IP.
        entities = [{"type": "code", "offset": len(notification[:match.start(1)].encode("utf-16-le")) // 2,
                     "length": len(match[1].encode("utf-16-le")) // 2}]
        self.send(chat_id, notification, reply_markup=reply_markup, entities=entities)

    def send_online_notify_test(self, chat_id):
        series, servers_up = self.snapshot()
        subscribed = {row[0] for row in self.db.execute(
            "SELECT group_name FROM subscriptions WHERE chat_id=?", (chat_id,))}
        if not subscribed:
            self.send(chat_id, "🧪 上线通知测试\n\n尚未设置订阅分组，请先新增分组。")
            return
        rows = sorted((item for item in series.values()
                       if item["server"] in servers_up
                       and item["group"] in subscribed),
                      key=lambda item: (item["group"], item["server"], item["email"]))
        if not rows:
            self.send(chat_id, "🧪 上线通知测试\n\n已订阅分组中没有客户端数据，无法生成测试样例。")
            return
        self.send_online_notification(chat_id, self.online_notification_text(rows[0], test=True))

    def process_traffic_notifications(self, now=None):
        now = time.time() if now is None else now
        local_now = datetime.fromtimestamp(now, timezone(timedelta(hours=8)))
        current_date = local_now.strftime("%Y-%m-%d")
        current_time = local_now.strftime("%H:%M")
        series, servers_up = self.snapshot()
        if not servers_up:
            return
        totals = self.traffic_totals(series, servers_up)
        settings = self.db.execute(
            "SELECT chat_id,notify_time FROM traffic_notify_settings WHERE enabled=1").fetchall()
        for chat_id, notify_time in settings:
            if current_time < notify_time:
                continue
            run = self.db.execute("SELECT sent_date FROM traffic_notify_runs WHERE chat_id=?", (chat_id,)).fetchone()
            if run and run[0] == current_date:
                continue
            groups = [row[0] for row in self.db.execute(
                "SELECT group_name FROM traffic_notify_groups WHERE chat_id=? ORDER BY group_name", (chat_id,))]
            if not groups:
                continue
            rows, current_values = self.traffic_notification_rows(chat_id, groups, totals)
            text = (f"📊 分组流量日报\n🕒 {current_date} {notify_time}（UTC+8）\n\n"
                    + "\n\n".join(rows) + self.report_footer(servers_up))
            try:
                self.send(chat_id, text)
            except ERRORS as error:
                print(f"Telegram 流量通知发送失败 ({error_kind(error)})", file=sys.stderr, flush=True)
                continue
            with self.db:
                self.db.executemany(
                    "INSERT INTO traffic_notify_baselines VALUES (?,?,?,?,?) "
                    "ON CONFLICT(chat_id,group_name,server) DO UPDATE SET up=excluded.up,down=excluded.down",
                    [(chat_id, group, server, up, down) for group, server, up, down in current_values])
                self.db.execute("INSERT OR REPLACE INTO traffic_notify_runs VALUES (?,?)", (chat_id, current_date))

    def process_online(self):
        series, servers_up = self.snapshot()
        if not servers_up:
            return
        baseline = self.db.execute("SELECT value FROM metadata WHERE key='baseline'").fetchone()
        changes = []
        with self.db:
            current_keys = set(series)
            existing_states = {
                (server, email): (group_name, online)
                for server, email, group_name, online in self.db.execute(
                    "SELECT server,email,group_name,online FROM state")
            }
            existing_observations = {
                (server, email): (float(last_sample_at), int(consecutive_online), start_traffic, first_seen_at)
                for server, email, last_sample_at, consecutive_online, start_traffic, first_seen_at in self.db.execute(
                    "SELECT server,email,last_sample_at,consecutive_online,start_traffic,first_seen_at FROM online_confirmations")
            }
            worker_map = {worker.panel["name"]: worker for worker in self.workers}
            missing_keys = [
                key for key in existing_states
                if key[0] in servers_up and key not in current_keys
            ]
            if missing_keys:
                self.db.executemany("UPDATE state SET online=0 WHERE server=? AND email=?", missing_keys)
                self.db.executemany(
                    "UPDATE online_confirmations SET consecutive_online=0, start_traffic=NULL, first_seen_at=NULL WHERE server=? AND email=?",
                    missing_keys)
            confirmation_updates = []
            state_updates = []
            for key, item in series.items():
                if item["server"] not in servers_up:
                    continue
                old = existing_states.get(key)
                sample_at = item.get("sample_at")
                if sample_at is None:
                    # A worker sample must carry the exporter success timestamp;
                    # without it, repeated Telegram polls could count one sample twice.
                    continue
                observation = existing_observations.get(key)
                if observation is not None and observation[0] == float(sample_at):
                    continue
                online = item["online"] == 1
                total_traffic = item["up"] + item["down"]
                start_traffic = None
                first_seen_at = None
                target_samples = self.online_confirmation_samples
                if baseline is None:
                    streak = target_samples if online else 0
                    if online:
                        first_seen_at = float(sample_at)
                elif observation is None:
                    # Existing state from before confirmation support is treated
                    # as a baseline when it was already online.
                    if online and old and old[1] == 1:
                        streak = target_samples
                        first_seen_at = float(sample_at)
                    elif online:
                        streak = 1
                        start_traffic = total_traffic
                        first_seen_at = float(sample_at)
                    else:
                        streak = 0
                elif online:
                    prev_streak = observation[1]
                    prev_start_traffic = observation[2]
                    prev_first_seen = observation[3]
                    if prev_streak == 0:
                        streak = 1
                        start_traffic = total_traffic
                        first_seen_at = float(sample_at)
                    else:
                        streak = min(prev_streak + 1, target_samples)
                        first_seen_at = float(prev_first_seen) if prev_first_seen is not None else float(sample_at)
                        if prev_start_traffic is not None and total_traffic < prev_start_traffic:
                            start_traffic = total_traffic
                        else:
                            start_traffic = prev_start_traffic
                    delta_bytes = (total_traffic - start_traffic) if start_traffic is not None else 0
                    worker = worker_map.get(key[0])
                    has_real_activity = (
                        worker is not None
                        and worker.has_recent_real_activity(item["email"], since_ts=first_seen_at)
                    )
                    fast_confirmed = (
                        prev_streak < target_samples
                        and streak >= TELEGRAM_ONLINE_FAST_CONFIRMATION_SAMPLES
                        and (delta_bytes >= TELEGRAM_ONLINE_FAST_CONFIRMATION_MIN_BYTES or has_real_activity)
                    )
                    if prev_streak == target_samples - 1 or fast_confirmed:
                        streak = target_samples
                        changes.append((key[0], item["email"], item["group"], first_seen_at, max(0, delta_bytes)))
                else:
                    streak = 0
                confirmation_updates.append((key[0], item["email"], sample_at, streak, start_traffic, first_seen_at))
                state_updates.append((*key, item["group"], item["online"]))
            if confirmation_updates:
                self.db.executemany(
                    "INSERT OR REPLACE INTO online_confirmations(server,email,last_sample_at,consecutive_online,start_traffic,first_seen_at) VALUES (?,?,?,?,?,?)",
                    confirmation_updates)
            if state_updates:
                self.db.executemany("INSERT OR REPLACE INTO state VALUES (?,?,?,?)", state_updates)
            if baseline is None:
                self.db.execute("INSERT INTO metadata VALUES ('baseline','1')")
                changes.clear()
        for server, email, group, first_seen_at, delta_bytes in changes:
            chats = [row[0] for row in self.db.execute(
                "SELECT subscriptions.chat_id FROM subscriptions "
                "LEFT JOIN online_notify_settings ON online_notify_settings.chat_id=subscriptions.chat_id "
                "WHERE subscriptions.group_name=? AND COALESCE(online_notify_settings.enabled,1)=1", (group,))]
            eligible_chats = []
            for chat_id in chats:
                now = time.time()
                muted = self.db.execute(
                    "SELECT until_at FROM client_mutes WHERE chat_id=? AND server=? AND email=?",
                    (chat_id, server, email)).fetchone()
                if muted is not None and now < float(muted[0]):
                    continue
                if self.cross_panel_cooldown:
                    last = self.db.execute(
                        "SELECT MAX(notified_at) FROM notification_cooldowns WHERE chat_id=? AND email=?",
                        (chat_id, email)).fetchone()
                else:
                    last = self.db.execute(
                        "SELECT notified_at FROM notification_cooldowns WHERE chat_id=? AND server=? AND email=?",
                        (chat_id, server, email)).fetchone()
                if (last is not None and last[0] is not None
                        and now - float(last[0]) < self.notification_cooldown_seconds):
                    continue
                eligible_chats.append(chat_id)
            if not eligible_chats:
                continue
            item = {"server": server, "email": email, "group": group,
                    "first_seen_at": first_seen_at, "delta_bytes": delta_bytes}
            notification = self.online_notification_text(item)
            for chat_id in eligible_chats:
                action_token = hashlib.sha256(f"{chat_id}\n{server}\n{email}".encode()).hexdigest()[:16]
                self.pending_notification_actions[(chat_id, action_token)] = (server, email, group)
                while len(self.pending_notification_actions) > 512:
                    self.pending_notification_actions.pop(next(iter(self.pending_notification_actions)))
                keyboard = {"inline_keyboard": [[
                    {"text": "📈 累计流量", "callback_data": f"ntf:traffic:{action_token}"},
                    {"text": "🔇 静默 6 小时", "callback_data": f"ntf:mute:{action_token}"},
                ]]}
                try:
                    self.send_online_notification(chat_id, notification, reply_markup=keyboard)
                except ERRORS as error:
                    print(f"Telegram 通知发送失败 ({error_kind(error)})", file=sys.stderr, flush=True)
                else:
                    recorded_at = time.time()
                    prune_before = recorded_at - max(self.notification_cooldown_seconds, 86400)
                    with self.db:
                        self.db.execute(
                            "INSERT OR REPLACE INTO notification_cooldowns VALUES (?,?,?,?)",
                            (chat_id, server, email, recorded_at))
                        self.db.execute(
                            "DELETE FROM notification_cooldowns WHERE notified_at < ?",
                            (prune_before,))
                        self.db.execute("DELETE FROM client_mutes WHERE until_at < ?", (recorded_at,))

    def handle_message(self, message):
        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")
        allowed_users = set(self.config.get("allowed_user_ids", []))
        allowed_chats = set(self.config.get("allowed_chat_ids", []))
        if not isinstance(chat_id, int):
            return
        text = (message.get("text") or "").strip()
        # Old reply keyboards send the entire label, including spaces.
        # Resolve it before parsing slash command arguments or pending prompts
        # so menu buttons and slash commands can always exit a pending time prompt.
        legacy_command = {label.replace("\ufe0f", ""): command
                          for label, command in self.BUTTON_COMMANDS.items()}.get(text.replace("\ufe0f", ""))
        if chat_id in self.pending_traffic_times and (
                user_id in allowed_users or chat_id in allowed_chats):
            if text.startswith("/") or legacy_command:
                self.pending_traffic_times.discard(chat_id)
            else:
                notify_time = normalize_notify_time(text)
                if notify_time is None:
                    self.send(chat_id, "时间格式无效，请发送例如 8:00 或 08:00（24 小时制）。")
                    return
                self.pending_traffic_times.discard(chat_id)
                self.set_traffic_notify_time(chat_id, notify_time)
                self.send_traffic_notify_settings(chat_id, "✅ 通知时间已更新\n\n")
                return
        if legacy_command:
            text = legacy_command
        command, _, argument = text.partition(" ")
        command = command.split("@", 1)[0].lower()
        argument = argument.strip()
        if command == "/id":
            self.send(chat_id, f"您的 Telegram ID：{user_id}\n当前 Chat ID：{chat_id}")
            return
        if user_id not in allowed_users and chat_id not in allowed_chats:
            if command in ("/start", "/help"):
                self.send(chat_id, "此机器人仅供授权用户使用。请先发送 /id 获取您的 Telegram 用户 ID。")
            return
        if legacy_command:
            self.send(chat_id, "菜单已更新，请使用聊天消息下方的按钮。", {"remove_keyboard": True})
            self.send(chat_id, "👇 请选择：", self.main_keyboard())
            if legacy_command == "/menu":
                return
        series, servers_up = self.snapshot()
        groups = sorted({item["group"] for item in series.values()})
        if command == "/start":
            name = message.get("from", {}).get("first_name") or "朋友"
            self.send(chat_id, f"👋 你好，{name}！\n🤖 欢迎使用 GLA 分组监控机器人。", {"remove_keyboard": True})
            self.send(chat_id, "👇 请选择：", self.main_keyboard())
            return
        elif command == "/menu":
            self.send(chat_id, "👇 请选择：", self.main_keyboard())
            return
        elif command == "/help":
            reply = ("🤖 欢迎使用 GLA 分组监控机器人！\n"
                     "您可以查询各面板的状态、分组在线客户端和累计流量，"
                     "也可以订阅指定分组的客户端上线通知，配置每日分组流量通知。\n\n"
                     "👇 请选择：")
            self.send(chat_id, reply, self.main_keyboard())
            return
        elif command == "/commands":
            reply = ("📖 命令说明\n\n"
                     "/start - 显示主菜单\n"
                     "/help - 显示功能介绍与操作菜单\n"
                     "/commands - 查看完整命令及用法\n"
                     "/status - 检查面板和在线状态\n"
                     "/groups - 查看客户端分组\n"
                     "/online [分组] - 查看在线客户端及流量\n"
                     "/traffic [分组] - 查看累计流量\n"
                     "/traffic_panels [分组] - 查看各分组在各面板的累计流量\n"
                     "/online_notify - 管理上线通知分组并发送测试\n"
                     "/watch [分组] - 订阅上线通知\n"
                     "/unwatch [分组] - 取消订阅\n"
                     "/watches - 查看上线与流量通知的分组订阅\n"
                     "/traffic_notify - 配置每日流量通知\n"
                     "/id - 显示您的 Telegram ID\n\n"
                     "[分组] 可省略；订阅和取消订阅时会显示分组选择按钮。\n"
                     "示例：/online Aurora 极光云瓶\n"
                     "流量为面板返回的累计值。")
            self.send(chat_id, reply, self.main_keyboard())
            return
        elif command == "/traffic_notify":
            if not argument:
                self.send_traffic_notify_settings(chat_id)
                return
            action, _, value = argument.partition(" ")
            value = value.strip()
            if action == "test" and not value:
                self.send_traffic_notify_test(chat_id)
                return
            if action == "groups" and not value:
                self.send_traffic_notify_groups(chat_id)
                return
            if action == "panels" and value in ("on", "off"):
                self.set_traffic_notify_panels(chat_id, value == "on")
                self.send_traffic_notify_settings(chat_id, "✅ 面板明细设置已更新\n\n")
                return
            if action in ("on", "off") and not value:
                enabled = int(action == "on")
                with self.db:
                    self.db.execute("INSERT INTO traffic_notify_settings(chat_id,enabled,notify_time) VALUES (?,?,'20:00') "
                                    "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled", (chat_id, enabled))
                self.send_traffic_notify_settings(chat_id, "✅ 定时流量通知设置已更新\n\n")
                return
            notify_time = normalize_notify_time(value) if action == "time" else None
            if notify_time is not None:
                self.set_traffic_notify_time(chat_id, notify_time)
                self.send_traffic_notify_settings(chat_id, "✅ 通知时间已更新\n\n")
                return
            if action in ("add", "remove") and value:
                if action == "add" and value not in groups:
                    self.send(chat_id, f"没有找到分组：{value}。使用 /groups 查看分组。")
                    return
                if action == "remove":
                    subscribed_traffic = {row[0] for row in self.db.execute(
                        "SELECT group_name FROM traffic_notify_groups WHERE chat_id=?", (chat_id,))}
                    if value not in groups and value not in subscribed_traffic:
                        self.send(chat_id, f"没有找到分组：{value}。使用 /groups 查看分组。")
                        return
                with self.db:
                    if action == "add":
                        self.db.execute("INSERT OR IGNORE INTO traffic_notify_groups VALUES (?,?)", (chat_id, value))
                    else:
                        self.db.execute("DELETE FROM traffic_notify_groups WHERE chat_id=? AND group_name=?", (chat_id, value))
                        self.db.execute("DELETE FROM traffic_notify_baselines WHERE chat_id=? AND group_name=?", (chat_id, value))
                self.send_traffic_notify_settings(chat_id, f"✅ {'已添加' if action == 'add' else '已移除'}分组：{value}\n\n")
                return
            self.send(chat_id, "用法：/traffic_notify、/traffic_notify test、/traffic_notify on、/traffic_notify off、"
                      "/traffic_notify time 20:00、/traffic_notify panels on|off、"
                      "/traffic_notify add 分组、/traffic_notify remove 分组")
            return
        elif command == "/online_notify":
            if argument == "test":
                self.send_online_notify_test(chat_id)
            elif argument == "groups":
                self.send_online_notify_groups(chat_id)
            elif argument in ("on", "off"):
                with self.db:
                    self.db.execute("INSERT INTO online_notify_settings VALUES (?,?) "
                                    "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled",
                                    (chat_id, int(argument == "on")))
                self.send_online_notify_settings(chat_id, "✅ 上线通知设置已更新\n\n")
            else:
                self.send_online_notify_settings(chat_id)
            return
        elif command == "/status":
            series, servers_up = self.snapshot()
            panel_names = [worker.panel["name"] for worker in self.workers]
            online_items = [item for item in series.values() if item["online"] and item["server"] in servers_up]
            panel_lines = [f"{'🟢' if name in servers_up else '🔴'} {name}：{'正常' if name in servers_up else '不可用'}" for name in panel_names]
            reply = ("📡 运行状态\n\n🤖 机器人运行正常\n"
                     f"🖥 面板：{len(servers_up)}/{len(panel_names)} 正常\n"
                     f"👥 在线分组：{len({item['group'] for item in online_items})}\n"
                     f"🟢 在线客户端：{len(online_items)}\n\n" + ("\n".join(panel_lines) if panel_lines else "尚未配置面板")
                     + "\n\n🕒 查询时间：" + self.report_time())
        elif command == "/groups":
            blocks = []
            for group in groups:
                members = [item for item in series.values() if item["group"] == group and item["server"] in servers_up]
                blocks.append(f"👥 分组：{group}\n🟢 在线：{sum(item['online'] for item in members)}/{len(members)} 个客户端")
            reply = "👥 客户端分组\n\n" + ("\n\n".join(blocks) if blocks else "📭 暂无分组数据") + self.report_footer(servers_up)
        elif command == "/online":
            rows = [item for item in series.values()
                    if item["online"] and item["server"] in servers_up and (not argument or item["group"] == argument)]
            blocks = [self.online_client_block(item)
                      for item in sorted(rows, key=lambda item: (item['group'], item['server'], item['email']))]
            reply = (f"🟢 在线客户端：{len(rows)} 个\n👥 筛选分组：{argument or '全部'}\n\n"
                     + ("\n\n".join(blocks) if blocks else "📭 没有匹配的在线客户端")
                     + "\n\nℹ️ 流量为客户端累计用量。" + self.report_footer(servers_up))
        elif command in ("/traffic", "/traffic_panels"):
            self.send_traffic_query(chat_id, argument, by_panel=command == "/traffic_panels")
            return
        elif command in ("/watch", "/unwatch") and not argument:
            if command == "/watch":
                choices = groups
                title = "🔔 请选择要订阅上线通知的分组："
            else:
                choices = [row[0] for row in self.db.execute(
                    "SELECT group_name FROM subscriptions WHERE chat_id=? ORDER BY group_name", (chat_id,))]
                title = "🔕 请选择要取消订阅的分组："
            self.pending_groups[chat_id] = (command, choices)
            buttons = [[{"text": group, "callback_data": f"{command[1:]}:{index}"}]
                       for index, group in enumerate(choices[:50])]
            if len(choices) > 50:
                title += "\n仅显示前 50 个；也可直接输入分组名称。"
            if not choices:
                reply = "目前没有可选分组。" if command == "/watch" else "目前没有已订阅分组。"
            else:
                self.send(chat_id, title, {"inline_keyboard": buttons})
                return
        elif command in ("/watch", "/unwatch") and argument:
            subscribed_online = {row[0] for row in self.db.execute(
                "SELECT group_name FROM subscriptions WHERE chat_id=?", (chat_id,))}
            if command == "/watch" and argument not in groups:
                reply = f"没有找到分组：{argument}。使用 /groups 查看分组。"
            elif command == "/unwatch" and argument not in groups and argument not in subscribed_online:
                reply = f"没有找到分组：{argument}。使用 /groups 查看分组。"
            elif command == "/watch":
                with self.db: self.db.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?)", (chat_id, argument))
                reply = f"🔔 已订阅分组：{argument}"
            else:
                with self.db: self.db.execute("DELETE FROM subscriptions WHERE chat_id=? AND group_name=?", (chat_id, argument))
                reply = f"🔕 已取消订阅：{argument}"
        elif command == "/watches":
            self.send_my_subscriptions(chat_id)
            return
        else:
            reply = "无法识别命令。发送 /help 查看命令。"
        self.send(chat_id, reply)

    def handle_callback(self, callback):
        callback_id = callback.get("id", "")
        message = callback.get("message", {})
        chat_id = message.get("chat", {}).get("id")
        user_id = callback.get("from", {}).get("id")
        allowed_users = set(self.config.get("allowed_user_ids", []))
        allowed_chats = set(self.config.get("allowed_chat_ids", []))
        if user_id not in allowed_users and chat_id not in allowed_chats:
            telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id, "text": "未授权"})
            return
        menu_commands = {"menu:home": "/start", "menu:status": "/status", "menu:groups": "/groups",
                         "menu:online": "/online", "menu:traffic": "/traffic",
                         "menu:online_notify": "/online_notify",
                         "menu:watch": "/watch", "menu:unwatch": "/unwatch",
                         "menu:watches": "/watches", "menu:help": "/help",
                         "menu:commands": "/commands", "menu:traffic_notify": "/traffic_notify"}
        data = callback.get("data", "")
        if data in menu_commands:
            telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
            self.handle_message({"chat": {"id": chat_id}, "from": {"id": user_id}, "text": menu_commands[data]})
            return
        match = re.fullmatch(r"ntf:(traffic|mute):([0-9a-f]{16})", data)
        if match:
            action, token = match.groups()
            target = self.pending_notification_actions.get((chat_id, token))
            if not target:
                telegram_request(self.config["token"], "answerCallbackQuery", {
                    "callback_query_id": callback_id, "text": "操作已过期，请使用 /online 或 /traffic 查询。"})
                return
            server, email, group = target
            if action == "mute":
                until_at = time.time() + CLIENT_MUTE_SECONDS
                with self.db:
                    self.db.execute("INSERT OR REPLACE INTO client_mutes VALUES (?,?,?,?)",
                                    (chat_id, server, email, until_at))
                telegram_request(self.config["token"], "answerCallbackQuery", {
                    "callback_query_id": callback_id, "text": f"已静默 {email} 6 小时"})
                self.send(chat_id, f"🔇 已静默客户端通知 6 小时：{email}（面板：{server}）")
                return
            telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
            series, servers_up = self.snapshot()
            item = series.get((server, email))
            if not item or server not in servers_up:
                self.send(chat_id, f"📈 客户端累计流量\n\n👤 客户端：{email}\n🖥 面板：{server}\n⚠️ 面板当前不可用或客户端已移除")
                return
            self.send(chat_id, f"📈 客户端累计流量\n\n{self.online_client_block(item)}\n\n🕒 查询时间：{self.report_time()}")
            return
        match = re.fullmatch(r"trafficview:(groups|panels):(all|[0-9a-f]{16})", data)
        if match:
            view, token = match.groups()
            argument = "" if token == "all" else self.pending_traffic_views.get((chat_id, token))
            if argument is None:
                telegram_request(self.config["token"], "answerCallbackQuery", {
                    "callback_query_id": callback_id, "text": "选项已过期，请重新查询累计流量。"})
                return
            telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
            self.send_traffic_query(chat_id, argument, by_panel=view == "panels")
            return
        match = re.fullmatch(r"onlinenotify:(toggle|groups|add|remove|test)(?::(\d+))?", data)
        if match:
            action, index_text = match.groups()
            if action == "toggle":
                if index_text not in ("0", "1"):
                    telegram_request(self.config["token"], "answerCallbackQuery", {
                        "callback_query_id": callback_id, "text": "无效的通知开关选项。"})
                    return
                with self.db:
                    self.db.execute("INSERT INTO online_notify_settings VALUES (?,?) "
                                    "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled",
                                    (chat_id, int(index_text)))
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
                self.send_online_notify_settings(chat_id, "✅ 上线通知设置已更新\n\n")
                return
            if action == "groups":
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
                self.send_online_notify_groups(chat_id)
                return
            if action == "test":
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
                self.send_online_notify_test(chat_id)
                return
            current = {row[0] for row in self.db.execute(
                "SELECT group_name FROM subscriptions WHERE chat_id=?", (chat_id,))}
            available = sorted(({item["group"] for item in self.snapshot()[0].values()} - current)
                               if action == "add" else current)
            if index_text is not None:
                index = int(index_text)
                pending = self.pending_online_groups.get(chat_id)
                if not pending or pending[0] != action or index >= len(pending[1]):
                    telegram_request(self.config["token"], "answerCallbackQuery", {
                        "callback_query_id": callback_id,
                        "text": "选项已过期，请重新打开上线通知设置。"})
                    return
                group = pending[1][index]
                with self.db:
                    if action == "add":
                        self.db.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?)", (chat_id, group))
                    else:
                        self.db.execute("DELETE FROM subscriptions WHERE chat_id=? AND group_name=?",
                                        (chat_id, group))
                self.pending_online_groups.pop(chat_id, None)
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
                self.send_online_notify_settings(
                    chat_id, f"✅ {'已添加' if action == 'add' else '已移除'}分组：{group}\n\n")
                return
            if not available:
                telegram_request(self.config["token"], "answerCallbackQuery", {
                    "callback_query_id": callback_id,
                    "text": "没有可选分组。" if action == "add" else "目前没有已订阅分组。"})
                return
            self.pending_online_groups[chat_id] = (action, available)
            prefix = "onlinenotify:add:" if action == "add" else "onlinenotify:remove:"
            buttons = [[{"text": group, "callback_data": f"{prefix}{index}"}]
                       for index, group in enumerate(available[:50])]
            telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
            title = "👥 请选择要订阅上线通知的分组：" if action == "add" else "👥 请选择要取消订阅的分组："
            self.send(chat_id, title, {"inline_keyboard": buttons})
            return
        match = re.fullmatch(r"trafficnotify:(toggle|panels|time|groups|test|add|remove)(?::(\d+))?", data)
        if match:
            action, index_text = match.groups()
            if action == "panels":
                if index_text not in ("0", "1"):
                    telegram_request(self.config["token"], "answerCallbackQuery", {
                        "callback_query_id": callback_id, "text": "无效的面板明细开关选项。"})
                    return
                self.set_traffic_notify_panels(chat_id, index_text == "1")
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
                self.send_traffic_notify_settings(chat_id, "✅ 面板明细设置已更新\n\n")
                return
            if action == "groups":
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
                self.send_traffic_notify_groups(chat_id)
                return
            if action == "test":
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
                self.send_traffic_notify_test(chat_id)
                return
            if action == "toggle":
                if index_text not in ("0", "1"):
                    telegram_request(self.config["token"], "answerCallbackQuery", {
                        "callback_query_id": callback_id, "text": "无效的通知开关选项。"})
                    return
                enabled = int(index_text)
                with self.db:
                    self.db.execute("INSERT INTO traffic_notify_settings(chat_id,enabled,notify_time) VALUES (?,?, '20:00') "
                                    "ON CONFLICT(chat_id) DO UPDATE SET enabled=excluded.enabled", (chat_id, enabled))
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
                self.send_traffic_notify_settings(chat_id, "✅ 定时流量通知设置已更新\n\n")
                return
            if action == "time":
                self.pending_traffic_times.add(chat_id)
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
                self.send(chat_id, "🕒 请在聊天中发送通知时间（北京时间，24 小时制），例如 20:00。")
                return
            series, _ = self.snapshot()
            current_groups = {row[0] for row in self.db.execute(
                "SELECT group_name FROM traffic_notify_groups WHERE chat_id=?", (chat_id,))}
            if action == "remove":
                groups = sorted(current_groups)
            else:
                groups = sorted({item["group"] for item in series.values()} - current_groups)
            if index_text is not None:
                index = int(index_text)
                pending = self.pending_traffic_groups.get(chat_id)
                if not pending or pending[0] != action or index >= len(pending[1]):
                    telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id,
                                                                                    "text": "选项已过期，请重新打开流量通知设置。"})
                    return
                group = pending[1][index]
                with self.db:
                    if action == "add":
                        self.db.execute("INSERT OR IGNORE INTO traffic_notify_groups VALUES (?,?)", (chat_id, group))
                    else:
                        self.db.execute("DELETE FROM traffic_notify_groups WHERE chat_id=? AND group_name=?", (chat_id, group))
                        self.db.execute("DELETE FROM traffic_notify_baselines WHERE chat_id=? AND group_name=?", (chat_id, group))
                self.pending_traffic_groups.pop(chat_id, None)
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
                self.send_traffic_notify_settings(chat_id, f"✅ {'已添加' if action == 'add' else '已移除'}分组：{group}\n\n")
                return
            if not groups:
                telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id,
                                                                                "text": "没有可选分组。"})
                return
            self.pending_traffic_groups[chat_id] = (action, groups)
            prefix = "trafficnotify:add:" if action == "add" else "trafficnotify:remove:"
            buttons = [[{"text": group, "callback_data": f"{prefix}{index}"}] for index, group in enumerate(groups[:50])]
            telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
            self.send(chat_id, "👥 请选择要添加到流量通知的分组：" if action == "add" else "👥 请选择要移除的分组：",
                      {"inline_keyboard": buttons})
            return
        match = re.fullmatch(r"(watch|unwatch):(\d+)", data)
        pending = self.pending_groups.get(chat_id)
        if not match or not pending or pending[0] != "/" + match[1]:
            telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id, "text": "选项已过期，请重新打开菜单。"})
            return
        index = int(match[2])
        if index >= len(pending[1]):
            telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id, "text": "选项已过期，请重新打开菜单。"})
            return
        group = pending[1][index]
        with self.db:
            if match[1] == "watch":
                self.db.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?)", (chat_id, group))
                reply = f"🔔 已订阅分组：{group}"
            else:
                self.db.execute("DELETE FROM subscriptions WHERE chat_id=? AND group_name=?", (chat_id, group))
                reply = f"🔕 已取消订阅：{group}"
        self.pending_groups.pop(chat_id, None)
        telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
        self.send(chat_id, reply)

    def run(self, stop_event=None):
        try:
            self.register_commands()
            print("Telegram command menu registered", flush=True)
        except ERRORS as error:
            print(f"Telegram 命令菜单注册失败 ({error_kind(error)})", file=sys.stderr, flush=True)
        next_check = 0
        try:
            while not (stop_event and stop_event.is_set()):
                if time.monotonic() >= next_check:
                    self.process_online()
                    self.process_traffic_notifications()
                    next_check = time.monotonic() + 15
                try:
                    updates = telegram_request(self.config["token"], "getUpdates", {"offset": self.offset, "timeout": 20}, timeout=30) or []
                    for update in updates:
                        self.offset = max(self.offset, int(update["update_id"]) + 1)
                        if "message" in update:
                            try:
                                self.handle_message(update["message"])
                            except ERRORS as error:
                                print(f"Telegram 命令处理失败 ({error_kind(error)})", file=sys.stderr, flush=True)
                        elif "callback_query" in update:
                            try:
                                self.handle_callback(update["callback_query"])
                            except ERRORS as error:
                                print(f"Telegram 按钮处理失败 ({error_kind(error)})", file=sys.stderr, flush=True)
                except ERRORS as error:
                    print(f"Telegram 连接失败 ({error_kind(error)})", file=sys.stderr, flush=True)
                    if stop_event is not None:
                        if stop_event.wait(5):
                            break
                    else:
                        time.sleep(5)
        finally:
            self.db.close()


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


def latest_ip_record(records):
    if isinstance(records, dict):
        records = records.get("ips", records.get("records", [records]))
    if isinstance(records, str):
        records = [records]
    if not isinstance(records, list) or not records:
        return None
    parsed = [item for item in (_parse_ip_record(record) for record in records) if item]
    if not parsed:
        return None
    # The panel does not guarantee newest-first order; unparseable times rank last.
    def rank(item):
        moment = parse_ip_record_time(item["time"])
        return moment.timestamp() if moment else float("-inf")
    return max(parsed, key=rank)  # max keeps the first of equal ranks


def _parse_ip_record(record):
    if isinstance(record, dict):
        raw_ip = str(record.get("ip") or record.get("IP") or "")
        timestamp = str(record.get("time") or record.get("timestamp") or record.get("date") or "")
    elif isinstance(record, str):
        raw_ip = record
        timestamp = ""
        match = re.search(r"\(([^()]*)\)\s*$", record)
        if match:
            timestamp = match.group(1).strip()
    else:
        return None
    candidates = re.findall(r"(?:\d{1,3}\.){3}\d{1,3}|[0-9A-Fa-f:]{3,}", raw_ip)
    for candidate in candidates:
        try:
            address = ipaddress.ip_address(candidate)
        except ValueError:
            continue
        return {"ip": str(address), "time": format_ip_record_time(timestamp)}
    return None


def format_ip_record_time(value):
    if not value:
        return ""
    parsed = parse_ip_record_time(value)
    if parsed is None:
        return str(value).strip()
    beijing = parsed.astimezone(timezone(timedelta(hours=8)))
    return beijing.strftime("%Y-%m-%d %H:%M:%S") + "（UTC+8）"


def parse_ip_record_time(value):
    if not value:
        return None
    raw = str(value).strip().replace("（UTC+8）", "").strip()
    try:
        if re.fullmatch(r"\d{10}(?:\d{3})?(?:\.\d+)?", raw):
            seconds = float(raw)
            parsed = datetime.fromtimestamp(seconds / 1000 if seconds >= 1e12 else seconds, timezone.utc)
        else:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return None
    # The panel's unzoned IP-log text is already Beijing local time. Treating
    # it as UTC adds eight hours again and can put a record after detection.
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone(timedelta(hours=8)))
    return parsed


def recent_ip_record(record, now=None, max_age=IP_RECORD_MAX_AGE_SECONDS):
    if not record or not record.get("time"):
        return None
    timestamp = parse_ip_record_time(record["time"])
    if timestamp is None:
        return None
    current = datetime.fromtimestamp(time.time() if now is None else now, timezone.utc)
    age = (current - timestamp).total_seconds()
    # Remote panel clocks may run slightly ahead of the central server.
    return record if -IP_RECORD_MAX_FUTURE_SKEW_SECONDS <= age <= max_age else None


def target_host_from_address(value):
    """Extract normalized target hostname/IP from Xray ToAddress."""
    text = str(value or "").strip().lower()
    if text.startswith(("tcp:", "udp:")):
        text = text[4:]
    if text.startswith("[") and "]" in text:
        return text[1:text.index("]")]
    if text.count(":") == 1:
        return text.rsplit(":", 1)[0]
    return text


def source_ip_from_address(value):
    """Extract an IP from Xray FromAddress such as 1.2.3.4:5, [::1]:5 or tcp:1.2.3.4:5."""
    text = str(value or "").strip()
    if text.startswith(("tcp:", "udp:")):
        text = text[4:]
    candidates = [text]
    if text.startswith("[") and "]" in text:
        candidates.insert(0, text[1:text.index("]")])
    elif text.count(":") == 1:
        candidates.insert(0, text.rsplit(":", 1)[0])
    elif text.count(":") > 1:
        candidates.append(text.rsplit(":", 1)[0])
    for candidate in candidates:
        try:
            return ipaddress.ip_address(candidate)
        except ValueError:
            continue
    return None


def mmdb_record(path, address):
    if maxminddb is None or not path or not Path(path).is_file():
        return {}
    with _MMDB_LOCK:
        reader = _MMDB_READERS.get(path)
        if reader is None:
            reader = maxminddb.open_database(path)
            _MMDB_READERS[path] = reader
    return reader.get(address) or {}


# User-confirmed corrections apply only to these exact IP addresses.
IP_ATTRIBUTION_OVERRIDES = {
    "2404:3881:10d:1000::6440:f22": ("香港", "V6VPS"),
}
NETWORK_ORGANIZATION_NAMES_ZH = {
    "lucidacloud limited": "野草云",
    "alibaba (us) technology co., ltd.": "阿里云",
    "hkt limited": "HKT香港电讯",
    "oracle corporation": "甲骨文云",
}
COUNTRY_NAMES_ZH = {
    "CN": "中国", "HK": "香港", "MO": "澳门", "TW": "台湾", "US": "美国",
    "JP": "日本", "SG": "新加坡", "GB": "英国", "DE": "德国", "KR": "韩国",
    "CA": "加拿大", "AU": "澳大利亚", "FR": "法国", "NL": "荷兰", "RU": "俄罗斯",
}
CHINA_SUBDIVISION_NAMES_ZH = {
    "AH": "安徽", "BJ": "北京", "CQ": "重庆", "FJ": "福建", "GS": "甘肃",
    "GD": "广东", "GX": "广西", "GZ": "贵州", "HI": "海南", "HE": "河北",
    "HL": "黑龙江", "HA": "河南", "HB": "湖北", "HN": "湖南", "NM": "内蒙古",
    "JS": "江苏", "JX": "江西", "JL": "吉林", "LN": "辽宁", "NX": "宁夏",
    "QH": "青海", "SN": "陕西", "SD": "山东", "SH": "上海", "SX": "山西",
    "SC": "四川", "TJ": "天津", "XJ": "新疆", "XZ": "西藏", "YN": "云南",
    "ZJ": "浙江",
    "Anhui": "安徽", "Beijing": "北京", "Chongqing": "重庆", "Fujian": "福建",
    "Gansu": "甘肃", "Guangdong": "广东", "Guangxi": "广西", "Guizhou": "贵州",
    "Hainan": "海南", "Hebei": "河北", "Heilongjiang": "黑龙江", "Henan": "河南",
    "Hong Kong": "香港", "Hubei": "湖北", "Hunan": "湖南", "Inner Mongolia": "内蒙古",
    "Jiangsu": "江苏", "Jiangxi": "江西", "Jilin": "吉林", "Liaoning": "辽宁",
    "Macau": "澳门", "Ningxia": "宁夏", "Qinghai": "青海", "Shaanxi": "陕西",
    "Shandong": "山东", "Shanghai": "上海", "Shanxi": "山西", "Sichuan": "四川",
    "Taiwan": "台湾", "Tianjin": "天津", "Tibet": "西藏", "Xinjiang": "新疆",
    "Yunnan": "云南", "Zhejiang": "浙江",
}


CARRIER_BY_ASN = {
    4134: "中国电信", 4812: "中国电信", 4809: "中国电信", 58519: "中国电信",
    4837: "中国联通", 9929: "中国联通", 17621: "中国联通", 134190: "中国联通",
    9808: "中国移动", 9394: "中国移动", 24400: "中国移动", 56040: "中国移动",
}
CARRIER_BY_ORG = (
    ("电信", "中国电信"), ("telecom", "中国电信"),
    ("chinanet", "中国电信"), ("联通", "中国联通"), ("unicom", "中国联通"),
    ("移动", "中国移动"), ("mobile", "中国移动"), ("cmnet", "中国移动"),
)


def ip_attribution(address):
    try:
        address = str(ipaddress.ip_address(address))
    except ValueError:
        pass
    if address in IP_ATTRIBUTION_OVERRIDES:
        return IP_ATTRIBUTION_OVERRIDES[address]
    try:
        city_data = mmdb_record(GEOIP_CITY_DB, address)
        asn_data = mmdb_record(GEOIP_ASN_DB, address)
    except Exception:
        city_data, asn_data = {}, {}
    country = city_data.get("country", {})
    iso_code = country.get("iso_code", "")
    asn = asn_data.get("autonomous_system_number")
    organization = asn_data.get("autonomous_system_organization", "")
    country_name = (country.get("names", {}).get("zh-CN")
                    or country.get("names", {}).get("en")
                    or COUNTRY_NAMES_ZH.get(iso_code, ""))
    if iso_code == "CN":
        city = (city_data.get("city", {}).get("names", {}).get("zh-CN")
                or city_data.get("city", {}).get("names", {}).get("en"))
        subdivisions = city_data.get("subdivisions", [])
        subdivision = subdivisions[0] if subdivisions and isinstance(subdivisions[0], dict) else {}
        province = (subdivision.get("names", {}).get("zh-CN")
                    or subdivision.get("names", {}).get("en")
                    or subdivision.get("iso_code", ""))
        province = CHINA_SUBDIVISION_NAMES_ZH.get(province, province)
        province = province.removesuffix("省")
        if province:
            location = f"{province}{city}" if city else f"中国{province}"
        else:
            location = "中国大陆"
    elif iso_code and COUNTRY_NAMES_ZH.get(iso_code):
        city = (city_data.get("city", {}).get("names", {}).get("zh-CN")
                or city_data.get("city", {}).get("names", {}).get("en"))
        country_name = COUNTRY_NAMES_ZH[iso_code]
        location = (country_name if iso_code in {"HK", "MO", "TW"}
                    else f"{country_name}·{city}" if city else country_name)
    elif country_name:
        city = (city_data.get("city", {}).get("names", {}).get("zh-CN")
                or city_data.get("city", {}).get("names", {}).get("en"))
        location = f"{country_name}·{city}" if city else country_name
    elif asn in CARRIER_BY_ASN:
        location = "中国大陆"
    else:
        location = "归属地未知"

    localized_organization = NETWORK_ORGANIZATION_NAMES_ZH.get(organization.strip().lower())
    if localized_organization:
        network = localized_organization
    elif iso_code == "CN" or asn in CARRIER_BY_ASN:
        network = CARRIER_BY_ASN.get(asn)
        org_lower = organization.lower()
        if network is None:
            network = next((name for text, name in CARRIER_BY_ORG if text in org_lower), None)
        network = network or "运营商未知"
    else:
        network = organization or (f"AS{asn}" if asn else "网络组织未知")
    return location, network


def online_url():
    return endpoint(API_URL, "/clients/onlines")


def client_group_map(clients):
    if not isinstance(clients, list) or any(not isinstance(c, dict) for c in clients):
        raise ValueError("客户端列表响应类型不正确")
    groups = {}
    for client in clients:
        if not client.get("email"):
            continue
        group = client.get("group") or "未分组"
        if not isinstance(group, str):
            raise ValueError("客户端分组响应类型不正确")
        groups[client["email"]] = group
    return groups


def inbound_client_map(inbounds):
    """Map each configured client email to all inbounds containing that client."""
    if not isinstance(inbounds, list):
        raise ValueError("入站列表响应类型不正确")
    result = {}
    for inbound in inbounds:
        if not isinstance(inbound, dict):
            continue
        settings = inbound.get("settings") or {}
        if isinstance(settings, str):
            try:
                settings = json.loads(settings)
            except (json.JSONDecodeError, TypeError):
                settings = {}
        clients = settings.get("clients", []) if isinstance(settings, dict) else []
        stats = inbound.get("clientStats") or []
        if not isinstance(clients, list):
            clients = []
        if not isinstance(stats, list):
            stats = []
        name = inbound.get("remark") or inbound.get("tag") or f"入站 {inbound.get('id', '未知')}"
        emails = {client.get("email") for client in [*clients, *stats]
                  if isinstance(client, dict) and isinstance(client.get("email"), str) and client["email"]}
        for email in emails:
            result.setdefault(email, set()).add(str(name))
    return {email: sorted(names) for email, names in result.items()}


def render_metrics(server, inbounds, online_emails, clients=None):
    if not isinstance(inbounds, list) or not isinstance(online_emails, list):
        raise ValueError("入站或在线列表响应类型不正确")
    online = set(online_emails)
    groups = client_group_map(clients) if clients is not None else {}
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
        tags = {"server": server, "email": email,
                "group": groups.get(email, "未分组" if clients is None else "未识别客户端")}
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


def normalize_log(entry, server, client_groups=None):
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
    tags = {"job": "xray-access", "server": server, "collector": "3x-ui-api",
            "group": (client_groups or {}).get(email, "未识别客户端")}
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

    def forward(self, entries, now=None, client_groups=None):
        if entries is None:
            entries = []
        if not isinstance(entries, list):
            raise ValueError("访问日志响应必须是数组")
        now = time.time() if now is None else now
        counts, records = Counter(), {}
        invalid = 0
        for entry in entries:
            try:
                digest, ns, tags, line = normalize_log(entry, self.panel["name"], client_groups)
                if not (now - self.panel.get("max_log_age", 3600)) * 1e9 <= ns <= (now + 300) * 1e9:
                    continue  # avoid Loki rejecting an entire batch of expired records
                counts[digest] += 1
                records[digest] = (ns, tags, line)
            except (ValueError, TypeError, AttributeError):
                invalid += 1
        had_previous = self.db.execute("SELECT 1 FROM seen LIMIT 1").fetchone() is not None
        existing = {}
        digests = list(counts)
        for offset in range(0, len(digests), 500):
            batch = digests[offset:offset + 500]
            placeholders = ",".join("?" for _ in batch)
            existing.update(self.db.execute(
                f"SELECT digest, occurrences FROM seen WHERE digest IN ({placeholders})", batch))
        streams, updates, overlap = {}, [], False
        for digest, count in counts.items():
            previous = existing.get(digest, 0)
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
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, socket.gaierror):
        return "DNS 解析失败"
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "TLS 证书校验失败"
    if isinstance(reason, ssl.SSLError):
        return "TLS 握手失败"
    if isinstance(reason, TimeoutError):
        return "连接或读取超时"
    if isinstance(reason, ConnectionResetError):
        return "连接被重置"
    if isinstance(reason, OSError):
        if reason.errno in (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.EAFNOSUPPORT):
            return "网络不可达（IPv6 面板请检查容器 IPv6 出站网络）"
        if reason.errno == errno.ECONNREFUSED:
            return "连接被拒绝"
        if reason.errno == errno.ECONNRESET:
            return "连接被重置"
    if isinstance(error, urllib.error.URLError) and reason is not None:
        return f"网络错误 ({type(reason).__name__})"
    return type(error).__name__  # never print Token, panel URLs or response bodies


class PanelWorker:
    def __init__(self, panel):
        self.panel = panel
        self.client_groups = {}
        self.client_inbounds = {}
        self.access_cache = {}
        self.lock = threading.Lock()
        self.up = False
        self.last_error = ""
        self.last_success = 0.0
        self.metrics = [metric("xui_exporter_up", 0, server=panel["name"])]
        self.log_status = {"enabled": int(panel.get("logs", False)), "up": 0,
                           "last_success_timestamp_seconds": 0, "window_full": 0,
                           "window_gap": 0, "invalid_records": 0}

    def poll(self, forwarder=None):
        panel = self.panel
        token = None
        poll_error = ""
        poll_up = False
        try:
            token = read_token(panel["token_file"])
            inbounds = request_json(panel["url"], token=token)
            mapped_inbounds = inbound_client_map(inbounds or [])
            clients = request_json(endpoint(panel["url"], "/clients/list"), token=token)
            mapped_groups = client_group_map(clients or [])
            online = request_json(endpoint(panel["url"], "/clients/onlines"), "POST", token=token)
            data = render_metrics(panel["name"], inbounds or [], online or [], clients or [])
            poll_up = True
        except ERRORS as error:
            poll_error = error_kind(error)
            data = [metric("xui_exporter_up", 0, server=panel["name"])]
            print(f'{panel["name"]}: 流量采集失败 ({poll_error})', file=sys.stderr, flush=True)
        with self.lock:
            self.metrics = data  # discard stale client series on failure
            self.up = poll_up
            self.last_error = poll_error
            if poll_up:
                self.last_success = time.time()
            if "mapped_inbounds" in locals():
                self.client_inbounds = mapped_inbounds
            if "mapped_groups" in locals():
                self.client_groups = mapped_groups
            current_groups = dict(self.client_groups)
        if not panel.get("logs", False):
            return
        try:
            if forwarder is None:
                raise RuntimeError("日志状态存储未就绪")
            if token is None:
                token = read_token(panel["token_file"])
            entries = request_json(endpoint(panel["url"], f'/server/xraylogs/{panel.get("log_count", 1000)}'),
                                   "POST", token=token, form={"showDirect": "true", "showBlocked": "true", "showProxy": "true"})
            self.record_access_entries(entries)
            stats = forwarder.forward(entries, client_groups=current_groups)
            with self.lock:
                self.log_status.update(stats, up=1, last_success_timestamp_seconds=time.time())
            if stats["window_gap"]:
                print(f'{panel["name"]}: 日志窗口已满且与上次没有重叠，可能漏采；增加窗口或缩短间隔', file=sys.stderr, flush=True)
        except ERRORS as error:
            with self.lock:
                self.log_status["up"] = 0
            print(f'{panel["name"]}: 日志采集或写入失败 ({error_kind(error)})', file=sys.stderr, flush=True)

    def run(self, stop_event=None):
        forwarder = None
        try:
            while not (stop_event and stop_event.is_set()):
                if self.panel.get("logs", False) and forwarder is None:
                    try:
                        forwarder = LogForwarder(self.panel)
                    except ERRORS as error:
                        print(f'{self.panel["name"]}: 日志状态存储失败 ({error_kind(error)})', file=sys.stderr, flush=True)
                start = time.monotonic()
                self.poll(forwarder)
                delay = max(1, self.panel.get("interval", 30) - (time.monotonic() - start))
                if stop_event is not None:
                    if stop_event.wait(delay):
                        break
                else:
                    time.sleep(delay)
        finally:
            if forwarder:
                forwarder.close()

    def snapshot(self):
        with self.lock:
            return self.metrics + [metric("xui_access_log_" + key, value, server=self.panel["name"])
                                   for key, value in self.log_status.items()]

    def inbounds_for_client(self, email):
        with self.lock:
            return list(self.client_inbounds.get(email, []))

    def record_access_entries(self, entries):
        """Remember each client's newest real source IP, inbound and non-probe activity."""
        if not isinstance(entries, list):
            return
        updates = {}
        for entry in entries:
            try:
                email = entry.get("Email") or ""
                if not email:
                    continue
                inbound = entry.get("Inbound") or ""
                if inbound == "api":
                    continue
                seconds = timestamp_ns(entry.get("DateTime", "")) / 1e9
                address = source_ip_from_address(entry.get("FromAddress"))
                target_host = target_host_from_address(entry.get("ToAddress"))
            except (ValueError, TypeError, AttributeError):
                continue
            # Loopback, private and unspecified sources (e.g. a local fallback or
            # reverse proxy) say nothing about the real client.
            usable = (address is not None and not (
                address.is_loopback or address.is_unspecified or address.is_link_local
                or address.is_multicast or any(address in net for net in LOCAL_SOURCE_NETWORKS
                                               if net.version == address.version)))
            is_real_target = bool(target_host) and target_host not in PROBE_TARGET_HOSTS
            current = updates.setdefault(email, {})
            if inbound and seconds >= current.get("inbound_at", float("-inf")):
                current.update(inbound=inbound, inbound_at=seconds)
            if usable and seconds >= current.get("ip_at", float("-inf")):
                current.update(ip=str(address), ip_at=seconds)
            elif address is not None and not usable and seconds >= current.get("local_ip_at", float("-inf")):
                current["local_ip_at"] = seconds
            if is_real_target and seconds >= current.get("real_activity_at", float("-inf")):
                current["real_activity_at"] = seconds
        with self.lock:
            for email, new in updates.items():
                old = self.access_cache.setdefault(email, {})
                if "ip" in new and new["ip_at"] >= old.get("ip_at", float("-inf")):
                    old.update(ip=new["ip"], ip_at=new["ip_at"])
                if "local_ip_at" in new and new["local_ip_at"] >= old.get("local_ip_at", float("-inf")):
                    old["local_ip_at"] = new["local_ip_at"]
                if "inbound" in new and new["inbound_at"] >= old.get("inbound_at", float("-inf")):
                    old.update(inbound=new["inbound"], inbound_at=new["inbound_at"])
                if "real_activity_at" in new and new["real_activity_at"] >= old.get("real_activity_at", float("-inf")):
                    old["real_activity_at"] = new["real_activity_at"]
            cutoff = time.time() - IP_RECORD_MAX_AGE_SECONDS
            for email in [e for e, v in self.access_cache.items()
                          if max(v.get("ip_at", 0), v.get("local_ip_at", 0),
                                 v.get("inbound_at", 0), v.get("real_activity_at", 0)) < cutoff]:
                del self.access_cache[email]

    def has_recent_real_activity(self, email, since_ts=None):
        with self.lock:
            activity_at = self.access_cache.get(email, {}).get("real_activity_at")
        if activity_at is None:
            return False
        if since_ts is not None:
            return activity_at >= float(since_ts) - 30
        return time.time() - activity_at <= 600

    def has_local_source_only(self, email):
        with self.lock:
            cached = self.access_cache.get(email, {})
            local_at = cached.get("local_ip_at")
            ip_at = cached.get("ip_at")
        now = time.time()
        has_recent_public = ip_at is not None and (now - ip_at) <= IP_RECORD_MAX_AGE_SECONDS
        return not has_recent_public and local_at is not None and (now - local_at) <= IP_RECORD_MAX_AGE_SECONDS

    def last_inbound_for_client(self, email):
        with self.lock:
            cached = self.access_cache.get(email, {})
            if cached.get("inbound") and time.time() - cached.get("inbound_at", 0) <= IP_RECORD_MAX_AGE_SECONDS:
                return cached["inbound"]
        return None

    def last_ip_for_client(self, email):
        with self.lock:
            cached = dict(self.access_cache.get(email, {}))
        if cached.get("ip"):
            record = recent_ip_record({"ip": cached["ip"],
                                       "time": format_ip_record_time(str(int(cached["ip_at"])))})
            if record:
                return record
        if not self.panel.get("url") or not self.panel.get("token_file"):
            return None
        try:
            token = read_token(self.panel["token_file"])
            url = endpoint(self.panel["url"], f"/clients/ips/{quote(email, safe='')}")
            records = request_json(url, "POST", token=token)
            return recent_ip_record(latest_ip_record(records))
        except ERRORS as error:
            print(f'{self.panel["name"]}: 客户端 IP 日志获取失败 ({error_kind(error)})',
                  file=sys.stderr, flush=True)
            return None


WORKERS = []


class RuntimeManager:
    """Coordinate hot-reloading of PanelWorker and TelegramBot threads."""

    def __init__(self, root_stop=None):
        self.root_stop = root_stop or threading.Event()
        self.lock = threading.Lock()
        self.worker_entries = []  # list of (panel_dict, PanelWorker, stop_event, thread)
        self.bot_stop = None
        self.bot_thread = None
        self.bot_instance = None

    def reload_panels(self):
        if not PANELS_FILE:
            return
        panels = load_config(PANELS_FILE)
        with self.lock:
            existing_by_name = {p["name"]: (p, w, stop, t) for p, w, stop, t in self.worker_entries}
            new_entries = []
            to_stop = []
            desired_names = {p["name"] for p in panels}
            for name, entry in existing_by_name.items():
                if name not in desired_names:
                    to_stop.append(entry)
            for panel in panels:
                prev = existing_by_name.get(panel["name"])
                if prev and prev[0] == panel and prev[3].is_alive():
                    new_entries.append(prev)
                    continue
                if prev:
                    to_stop.append(prev)
                worker = PanelWorker(panel)
                stop_event = threading.Event()
                thread = threading.Thread(target=worker.run, args=(stop_event,), daemon=True, name=panel["name"])
                thread.start()
                new_entries.append((dict(panel), worker, stop_event, thread))
            self.worker_entries = new_entries
            WORKERS[:] = [entry[1] for entry in new_entries]
            if self.bot_instance is not None:
                self.bot_instance.workers = list(WORKERS)
        for _, _, stop_event, thread in to_stop:
            stop_event.set()
            thread.join(timeout=2)

    def reload_telegram(self):
        with self.lock:
            old_stop = self.bot_stop
            old_thread = self.bot_thread
            self.bot_stop = None
            self.bot_thread = None
            self.bot_instance = None
        if old_stop is not None:
            old_stop.set()
        if old_thread is not None and old_thread.is_alive():
            old_thread.join(timeout=5)
        try:
            config = telegram_config()
            if not config:
                return
            bot_stop = threading.Event()
            bot = TelegramBot(config, list(WORKERS), STATE_DIR)
            thread = threading.Thread(target=bot.run, args=(bot_stop,), daemon=True, name="telegram-bot")
            thread.start()
            with self.lock:
                self.bot_stop = bot_stop
                self.bot_thread = thread
                self.bot_instance = bot
            print("Telegram Bot started", flush=True)
        except ERRORS as error:
            print(f"Telegram 配置无效，Bot 未启动 ({error_kind(error)})", file=sys.stderr, flush=True)

    def stop_all(self):
        self.root_stop.set()
        with self.lock:
            entries = list(self.worker_entries)
            bot_stop = self.bot_stop
            bot_thread = self.bot_thread
        if bot_stop is not None:
            bot_stop.set()
        for _, _, stop_event, _ in entries:
            stop_event.set()
        if bot_thread is not None and bot_thread.is_alive():
            bot_thread.join(timeout=3)
        for _, _, _, thread in entries:
            thread.join(timeout=3)


RUNTIME = None
DEFAULT_WEB_ADMIN_USERNAME = "gla"
DEFAULT_WEB_ADMIN_PASSWORD = "gla12345"
ADMIN_SESSION_TTL_SECONDS = 12 * 3600
ADMIN_LOGIN_MAX_ATTEMPTS = 5
ADMIN_LOGIN_WINDOW_SECONDS = 300
_ADMIN_LOGIN_LOCK = threading.Lock()
_ADMIN_LOGIN_FAILURES = {}
_WEB_ADMIN_INIT_LOCK = threading.Lock()


def web_admin_config(path=None):
    config_path = Path(path or WEB_ADMIN_CONFIG_FILE)
    with _WEB_ADMIN_INIT_LOCK:
        if not config_path.is_file():
            hashed = hash_admin_password(DEFAULT_WEB_ADMIN_PASSWORD)
            default_cfg = {
                "enabled": True,
                "username": DEFAULT_WEB_ADMIN_USERNAME,
                "default_password": True,
                **hashed,
                "session_secret": secrets.token_hex(32),
            }
            atomic_write(config_path, json.dumps(default_cfg, ensure_ascii=False, indent=2) + "\n")
            return default_cfg
    config = json.loads(config_path.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError("Web 管理配置无效")
    if config.get("enabled") is False:
        return None
    if (not isinstance(config.get("password_salt"), str)
            or not isinstance(config.get("password_hash"), str)
            or not isinstance(config.get("session_secret"), str)):
        raise ValueError("Web 管理配置无效")
    return {"enabled": True, "username": config.get("username") or DEFAULT_WEB_ADMIN_USERNAME, **config}


def hash_admin_password(password, salt_hex=None, iterations=200000):
    if not isinstance(password, str) or len(password) < 8:
        raise ValueError("管理员密码长度不能少于 8 位")
    salt = bytes.fromhex(salt_hex) if salt_hex else secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return {"password_salt": salt.hex(), "password_hash": digest.hex(), "iterations": iterations}


def verify_admin_password(password, config):
    if not isinstance(password, str) or not password or not isinstance(config, dict):
        return False
    try:
        salt = bytes.fromhex(config["password_salt"])
        expected = bytes.fromhex(config["password_hash"])
        iterations = int(config.get("iterations", 200000))
    except (KeyError, ValueError, TypeError):
        return False
    actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)
    return hmac.compare_digest(actual, expected)


def save_web_admin_password(password, path=None, session_secret=None, username=None, default_password=False):
    target = Path(path or WEB_ADMIN_CONFIG_FILE)
    existing = None
    if target.is_file():
        raw = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            existing = raw
    resolved_user = (username if username is not None else (existing.get("username") if existing else None)) or DEFAULT_WEB_ADMIN_USERNAME
    if not NAME_RE.fullmatch(resolved_user):
        raise ValueError("管理员用户名只能包含字母、数字、点、下划线和连字符")
    hashed = hash_admin_password(password)
    secret = session_secret or (existing.get("session_secret") if existing else None) or secrets.token_hex(32)
    atomic_write(target, json.dumps({
        "enabled": True,
        "username": resolved_user,
        "default_password": bool(default_password),
        **hashed,
        "session_secret": secret,
    }, ensure_ascii=False, indent=2) + "\n")


def web_admin_cli(argv):
    parser = argparse.ArgumentParser(description="管理 GLA Web 配置台")
    parser.add_argument("operation", choices=("set-password", "disable", "status"))
    parser.add_argument("--username")
    args = parser.parse_args(argv)
    target = Path(WEB_ADMIN_CONFIG_FILE)
    if args.operation == "status":
        cfg = web_admin_config(target)
        if not cfg:
            print("已关闭 Web 配置台")
        elif cfg.get("default_password"):
            print(f"已启用 Web 配置台（账户：{cfg['username']}，初始密码：{DEFAULT_WEB_ADMIN_PASSWORD}，请及时修改）")
        else:
            print(f"已启用 Web 配置台（账户：{cfg['username']}，已修改自定义密码）")
        return
    if args.operation == "disable":
        atomic_write(target, json.dumps({"enabled": False}, indent=2) + "\n")
        print("已关闭 Web 配置台")
        return
    password = sys.stdin.read().rstrip("\r\n")
    save_web_admin_password(password, target, username=args.username, default_password=False)
    print("Web 配置台账户与密码已更新")


def _b64u_encode(raw):
    import base64
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64u_decode(text):
    import base64
    padded = text + "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(padded.encode("ascii"))


def issue_admin_session(config, now=None):
    now = time.time() if now is None else now
    payload = {"exp": int(now + ADMIN_SESSION_TTL_SECONDS), "csrf": secrets.token_urlsafe(24)}
    encoded = _b64u_encode(json.dumps(payload, separators=(",", ":")).encode("utf-8"))
    sig = hmac.new(config["session_secret"].encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
    return f"{encoded}.{_b64u_encode(sig)}", payload["csrf"]


def verify_admin_session(cookie_header, config, now=None):
    if not cookie_header or not config:
        return None
    now = time.time() if now is None else now
    token = ""
    for part in cookie_header.split(";"):
        name, _, value = part.strip().partition("=")
        if name == "gla_admin_session":
            token = value.strip()
            break
    if "." not in token:
        return None
    encoded, sig_b64 = token.split(".", 1)
    try:
        expected_sig = hmac.new(config["session_secret"].encode("utf-8"), encoded.encode("ascii"), hashlib.sha256).digest()
        actual_sig = _b64u_decode(sig_b64)
        if not hmac.compare_digest(actual_sig, expected_sig):
            return None
        payload = json.loads(_b64u_decode(encoded).decode("utf-8"))
        if int(payload.get("exp", 0)) < now or not payload.get("csrf"):
            return None
        return payload
    except (ValueError, TypeError, KeyError, json.JSONDecodeError):
        return None


def _admin_login_allowed(client_ip, now=None):
    now = time.time() if now is None else now
    with _ADMIN_LOGIN_LOCK:
        attempts = [ts for ts in _ADMIN_LOGIN_FAILURES.get(client_ip, []) if now - ts < ADMIN_LOGIN_WINDOW_SECONDS]
        if attempts:
            _ADMIN_LOGIN_FAILURES[client_ip] = attempts
        else:
            _ADMIN_LOGIN_FAILURES.pop(client_ip, None)
        return len(attempts) < ADMIN_LOGIN_MAX_ATTEMPTS


def _record_admin_login_failure(client_ip, now=None):
    now = time.time() if now is None else now
    with _ADMIN_LOGIN_LOCK:
        attempts = [ts for ts in _ADMIN_LOGIN_FAILURES.get(client_ip, []) if now - ts < ADMIN_LOGIN_WINDOW_SECONDS]
        attempts.append(now)
        _ADMIN_LOGIN_FAILURES[client_ip] = attempts


def _clear_admin_login_failures(client_ip):
    with _ADMIN_LOGIN_LOCK:
        _ADMIN_LOGIN_FAILURES.pop(client_ip, None)


class MetricsHandler(BaseHTTPRequestHandler):
    def _send_json(self, status, payload, extra_headers=None):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra_headers or []):
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0 or length > 65536:
            raise ValueError("请求体大小无效")
        data = json.loads(self.rfile.read(length).decode("utf-8"))
        if not isinstance(data, dict):
            raise ValueError("请求体必须是 JSON 对象")
        return data

    def _admin_client_ip(self):
        real_ip = (self.headers.get("X-Real-IP") or "").strip()
        if real_ip:
            return real_ip
        forwarded = (self.headers.get("X-Forwarded-For") or "").strip()
        if forwarded:
            return forwarded.split(",")[-1].strip()
        return self.client_address[0] if self.client_address else "unknown"

    def _cookie_attrs(self):
        attrs = "HttpOnly; SameSite=Strict; Path=/"
        if (self.headers.get("X-Forwarded-Proto") or "").strip().lower() == "https":
            attrs += "; Secure"
        return attrs

    def _require_admin(self, require_csrf=False):
        config = web_admin_config()
        if not config:
            self._send_json(404, {"error": "Web 配置台未启用"})
            return None, None
        session = verify_admin_session(self.headers.get("Cookie", ""), config)
        if not session:
            self._send_json(401, {"error": "未登录或会话已过期"})
            return None, None
        if require_csrf:
            supplied = self.headers.get("X-CSRF-Token", "")
            if not supplied or not hmac.compare_digest(supplied, session["csrf"]):
                self._send_json(403, {"error": "CSRF 校验失败"})
                return None, None
        return config, session

    def do_DELETE(self):
        path = urllib.parse.urlsplit(self.path).path
        if path.startswith("/api/admin/"):
            self.handle_admin_mutation("DELETE", path)
            return
        self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlsplit(self.path).path
        if path == "/api/ssh-login":
            try:
                self.handle_ssh_login()
            except (ValueError, TypeError, KeyError, OSError, RuntimeError, sqlite3.Error) as error:
                print(f"SSH 登录事件处理失败 ({error_kind(error)})", file=sys.stderr, flush=True)
                self.send_error(400, "invalid event")
            return
        if path.startswith("/api/admin/"):
            self.handle_admin_mutation("POST", path)
            return
        self.send_error(404)

    def handle_ssh_login(self):
        config_path = Path(SSH_LOGIN_CONFIG_FILE)
        if not config_path.is_file():
            self.send_error(503, "SSH login intake is not configured")
            return
        config = json.loads(config_path.read_text(encoding="utf-8"))
        servers = config.get("servers", {}) if isinstance(config, dict) else {}
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 4096:
            self.send_error(413)
            return
        fields = urllib.parse.parse_qs(self.rfile.read(length).decode("utf-8"), strict_parsing=True)
        event = {key: values[0] for key, values in fields.items() if values}
        server = event.get("server", "")
        server_config = servers.get(server) if isinstance(servers, dict) else None
        supplied_key = self.headers.get("X-GLA-Key", "")
        if (not isinstance(server_config, dict) or not isinstance(server_config.get("key"), str)
                or not hmac.compare_digest(supplied_key, server_config["key"])):
            self.send_error(403)
            return
        username = event.get("user", "")
        try:
            address = ipaddress.ip_address(event.get("source_ip", ""))
        except ValueError:
            self.send_error(400, "invalid source IP")
            return
        if (not username or len(username) > 128 or any(ord(char) < 32 for char in username)
                or not NAME_RE.fullmatch(server)):
            self.send_error(400, "invalid login fields")
            return
        now_ns = time.time_ns()
        event_time = event.get("event_time", "")
        try:
            parsed_time = datetime.fromisoformat(event_time.replace("Z", "+00:00"))
            if parsed_time.tzinfo is None:
                raise ValueError("timezone is required")
            timestamp = int(parsed_time.timestamp() * 1_000_000_000)
            if abs(timestamp - now_ns) > 300_000_000_000:
                timestamp = now_ns
        except ValueError:
            timestamp = now_ns
        try:
            location, network = ip_attribution(str(address))
        except Exception:
            location, network = "归属地未知", "网络组织未知"
        record = {"event": "ssh_login", "server": server, "user": username,
                  "source_ip": str(address), "source_port": event.get("source_port", ""),
                  "ssh_port": event.get("ssh_port", "22"),
                  "auth_method": event.get("auth_method", "unknown"),
                  "location": location, "network": network}
        loki_ok = True
        try:
            push_loki([{"stream": {"job": "ssh-login", "server": server},
                       "values": [[str(timestamp), json.dumps(record, ensure_ascii=False, separators=(",", ":"))]]}])
        except ERRORS as error:
            loki_ok = False
            print(f"SSH 登录事件 Loki 写入失败 ({error_kind(error)})", file=sys.stderr, flush=True)
        ip_groups = config.get("ip_groups", {}) if isinstance(config, dict) else {}
        allowed_cidrs = list(server_config.get("allowed_cidrs", []))
        for group_name in server_config.get("allowed_ip_groups", []):
            group_cidrs = ip_groups.get(group_name, []) if isinstance(ip_groups, dict) else []
            if isinstance(group_cidrs, list):
                allowed_cidrs.extend(group_cidrs)
        if not ssh_login_ip_allowed(address, allowed_cidrs):
            notify_ssh_login(record)
        self.send_response(204 if loki_ok else 503)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlsplit(self.path).path
        if path in ("/", "/admin"):
            if not web_admin_config() or not Path(ADMIN_HTML_FILE).is_file():
                self.send_error(404)
                return
            if path == "/":
                self.send_response(302)
                self.send_header("Location", "/admin")
                self.end_headers()
                return
            body = Path(ADMIN_HTML_FILE).read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass
            return
        if path == "/api/admin/session":
            config = web_admin_config()
            if not config:
                self._send_json(404, {"error": "Web 配置台未启用"})
                return
            session = verify_admin_session(self.headers.get("Cookie", ""), config)
            self._send_json(200, {"authenticated": bool(session),
                                  "csrf_token": session["csrf"] if session else ""})
            return
        if path == "/api/admin/overview":
            config, _ = self._require_admin(require_csrf=False)
            if not config:
                return
            self._send_json(200, admin_overview_payload(config))
            return
        if self.path not in ("/metrics", "/metrics?"):
            self.send_error(404)
            return
        if self.headers.get("X-Forwarded-For") or self.headers.get("X-Real-IP"):
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

    def handle_admin_mutation(self, method, path):
        if path == "/api/admin/login" and method == "POST":
            config = web_admin_config()
            if not config:
                self._send_json(404, {"error": "Web 配置台未启用"})
                return
            client_ip = self._admin_client_ip()
            if not _admin_login_allowed(client_ip):
                self._send_json(429, {"error": "登录失败次数过多，请 5 分钟后再试"})
                return
            try:
                body = self._read_json_body()
            except (ValueError, json.JSONDecodeError) as error:
                self._send_json(400, {"error": str(error)})
                return
            supplied_user = str(body.get("username", ""))
            expected_user = config.get("username", DEFAULT_WEB_ADMIN_USERNAME)
            if (not hmac.compare_digest(supplied_user, expected_user)
                    or not verify_admin_password(body.get("password", ""), config)):
                _record_admin_login_failure(client_ip)
                self._send_json(401, {"error": "用户名或密码错误"})
                return
            _clear_admin_login_failures(client_ip)
            cookie_val, csrf = issue_admin_session(config)
            self._send_json(200, {"authenticated": True, "csrf_token": csrf}, [
                ("Set-Cookie", f"gla_admin_session={cookie_val}; Max-Age={ADMIN_SESSION_TTL_SECONDS}; {self._cookie_attrs()}")
            ])
            return
        if path == "/api/admin/logout" and method == "POST":
            self._send_json(200, {"ok": True}, [
                ("Set-Cookie", f"gla_admin_session=; Max-Age=0; {self._cookie_attrs()}")
            ])
            return
        config, _ = self._require_admin(require_csrf=True)
        if not config:
            return
        try:
            body = self._read_json_body()
            result = apply_admin_action(method, path, body, config)
            self._send_json(200, result)
        except (ValueError, TypeError, KeyError, OSError, RuntimeError, json.JSONDecodeError) as error:
            self._send_json(400, {"error": str(error)})

    def log_message(self, *_args):
        pass


def ssh_login_ip_allowed(address, cidrs):
    for cidr in cidrs:
        try:
            if address in ipaddress.ip_network(cidr, strict=False):
                return True
        except ValueError:
            continue
    return False


_SSH_LOGIN_NOTIFY_LOCK = threading.Lock()
_SSH_LOGIN_IN_FLIGHT = set()


def _ssh_login_cooldown(config):
    cooldown = config.get("notify_cooldown_seconds", 600)
    if type(cooldown) is not int or cooldown < 0 or cooldown > 86400:
        cooldown = 600
    return cooldown


def _ssh_login_is_allowed_locked(record, cooldown):
    path = Path(STATE_DIR) / "ssh-login.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    try:
        db.execute("CREATE TABLE IF NOT EXISTS notifications (server TEXT, source_ip TEXT, user TEXT, notified_at REAL NOT NULL, PRIMARY KEY(server,source_ip,user))")
        now = time.time()
        row = db.execute("SELECT notified_at FROM notifications WHERE server=? AND source_ip=? AND user=?",
                         (record["server"], record["source_ip"], record["user"])).fetchone()
        if row is not None and cooldown and now - float(row[0]) < cooldown:
            return False
    finally:
        db.close()
    return True


def ssh_login_notify_allowed(record, config):
    cooldown = _ssh_login_cooldown(config)
    with _SSH_LOGIN_NOTIFY_LOCK:
        return _ssh_login_is_allowed_locked(record, cooldown)


def _record_ssh_login_notification_locked(record):
    path = Path(STATE_DIR) / "ssh-login.sqlite"
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(path), timeout=10)
    try:
        with db:
            db.execute("CREATE TABLE IF NOT EXISTS notifications (server TEXT, source_ip TEXT, user TEXT, notified_at REAL NOT NULL, PRIMARY KEY(server,source_ip,user))")
            now = time.time()
            db.execute("INSERT OR REPLACE INTO notifications VALUES (?,?,?,?)",
                       (record["server"], record["source_ip"], record["user"], now))
            db.execute("DELETE FROM notifications WHERE notified_at < ?", (now - 7 * 86400,))
    finally:
        db.close()


def record_ssh_login_notification(record):
    with _SSH_LOGIN_NOTIFY_LOCK:
        _record_ssh_login_notification_locked(record)


def notify_ssh_login(record):
    config = telegram_config(SSH_TELEGRAM_CONFIG_FILE)
    if not config:
        return
    chats = sorted(set(config.get("allowed_chat_ids", []) + config.get("allowed_user_ids", [])))
    if not chats:
        return
    key = (record["server"], record["source_ip"], record["user"])
    cooldown = _ssh_login_cooldown(config)
    with _SSH_LOGIN_NOTIFY_LOCK:
        if key in _SSH_LOGIN_IN_FLIGHT or not _ssh_login_is_allowed_locked(record, cooldown):
            return
        _SSH_LOGIN_IN_FLIGHT.add(key)
    delivered = False
    try:
        text = ("🚨 陌生 IP SSH 登录\n\n"
                f"🖥 服务器：{record['server']}\n"
                f"👤 用户：{record['user']}\n"
                f"🔌 SSH 端口：{record.get('ssh_port', '未知')}\n"
                f"🌐 来源 IP：{record['source_ip']}\n"
                f"📍 归属地：{record['location']}\n"
                f"🏢 网络：{record['network']}\n"
                f"🔐 认证方式：{record['auth_method']}\n"
                f"🕒 时间：{datetime.now(timezone(timedelta(hours=8))):%Y-%m-%d %H:%M:%S}（UTC+8）")
        for chat_id in chats:
            try:
                telegram_request(config["token"], "sendMessage", {"chat_id": chat_id, "text": text}, timeout=10)
                delivered = True
            except ERRORS as error:
                print(f"SSH 登录 Telegram 通知失败 ({error_kind(error)})", file=sys.stderr, flush=True)
    finally:
        with _SSH_LOGIN_NOTIFY_LOCK:
            if delivered:
                try:
                    _record_ssh_login_notification_locked(record)
                except ERRORS as error:
                    print(f"SSH 登录通知冷却记录失败 ({error_kind(error)})", file=sys.stderr, flush=True)
            _SSH_LOGIN_IN_FLIGHT.discard(key)


def panels_cli(argv):
    parser = argparse.ArgumentParser(description="管理中心端 3x-ui 面板；Token 从标准输入读取")
    parser.add_argument("operation", choices=("list", "add", "update", "set", "delete", "migrate"))
    parser.add_argument("--format", choices=("display", "tsv"), default="display")
    parser.add_argument("--name")
    parser.add_argument("--new-name")
    parser.add_argument("--url")
    parser.add_argument("--logs", choices=("true", "false"))
    parser.add_argument("--interval", type=int)
    parser.add_argument("--log-count", type=int)
    args = parser.parse_args(argv)
    if not PANELS_FILE:
        raise ValueError("缺少 XUI_PANELS_FILE")
    panels = load_config(PANELS_FILE)
    if args.operation == "list":
        for index, panel in enumerate(panels, 1):
            logs = "true" if panel.get("logs", False) else "false"
            if args.format == "tsv":
                print(f'{panel["name"]}\t{panel["url"]}\t{logs}\t{panel.get("interval", 30)}\t{panel.get("log_count", 1000)}')
            else:
                state = "开启" if logs == "true" else "关闭"
                print(f'{index}. {panel["name"]}\t{panel["url"]}\t日志={state}\t间隔={panel.get("interval", 30)}s\t窗口={panel.get("log_count", 1000)}')
        return
    if not args.name or not NAME_RE.fullmatch(args.name):
        raise ValueError("请指定有效的面板名称")
    previous = next((p for p in panels if p["name"] == args.name), None)
    if args.operation == "add" and previous:
        raise ValueError("名称已存在，请选择修改面板")
    if args.operation == "update" and not previous:
        raise ValueError("要修改的面板不存在")
    remaining = [p for p in panels if p["name"] != args.name]
    if args.operation in ("add", "update", "set", "migrate"):
        token = sys.stdin.read().strip()
        if args.operation == "migrate" and previous:
            return  # preserve an already configured panel on repeated upgrades
        panel = dict(previous) if args.operation == "update" else {}
        name = args.new_name if args.operation == "update" and args.new_name is not None else args.name
        if any(p["name"] == name for p in remaining):
            raise ValueError("名称已存在，请使用其他名称")
        panel.update(name=name, token_file=f"/run/secrets/xui-{name}.token")
        for key, value, default in (("url", args.url, ""),
                                    ("logs", None if args.logs is None else args.logs == "true", True),
                                    ("interval", args.interval, 30), ("log_count", args.log_count, 1000)):
            if value is not None:
                panel[key] = value
            else:
                panel.setdefault(key, default)
        validate_panel(panel)
        token_path = Path(PANELS_FILE).parent / f"xui-{name}.token"
        if token:
            if "\n" in token or "\r" in token:
                raise ValueError("Token 不能包含换行符")
            atomic_write(token_path, token + "\n")
        elif not previous:
            raise ValueError("新面板必须提供 Token")
        else:
            old_token_path = Path(PANELS_FILE).parent / Path(previous["token_file"]).name
            old_token = read_token(old_token_path)
            if token_path != old_token_path:
                atomic_write(token_path, old_token + "\n")
        if previous:
            remaining = [panel if p["name"] == args.name else p for p in panels]
        else:
            remaining.append(panel)
    atomic_write(PANELS_FILE, json.dumps(remaining, ensure_ascii=False, indent=2) + "\n")
    if args.operation == "update" and previous and panel["name"] != args.name:
        (Path(PANELS_FILE).parent / Path(previous["token_file"]).name).unlink(missing_ok=True)
    if args.operation == "delete" and previous:
        (Path(PANELS_FILE).parent / f"xui-{args.name}.token").unlink(missing_ok=True)


def telegram_cli(argv, config_file=None, token_filename="telegram-bot.token",
                 test_message="✅ Telegram Bot 测试消息；接收配置正常。"):
    parser = argparse.ArgumentParser(description="管理 Telegram Bot 配置")
    parser.add_argument("operation", choices=("set", "delete", "show", "test"))
    parser.add_argument("--user-ids", default="")
    args = parser.parse_args(argv)
    path = Path(config_file or TELEGRAM_CONFIG_FILE)
    if args.operation == "show":
        config = telegram_config(path)
        if config:
            print("已配置 Telegram Bot；允许用户数：%d" % len(config["allowed_user_ids"]))
        else:
            print("未配置 Telegram Bot")
        return
    if args.operation == "test":
        config = telegram_config(path)
        if not config:
            raise ValueError("尚未配置 Telegram Bot")
        for user_id in config["allowed_user_ids"]:
            telegram_request(config["token"], "sendMessage", {
                "chat_id": user_id, "text": test_message})
        print("测试消息已发送")
        return
    if args.operation == "delete":
        path.unlink(missing_ok=True)
        path.parent.joinpath(token_filename).unlink(missing_ok=True)
        return
    try:
        user_ids = [int(value.strip()) for value in args.user_ids.split(",") if value.strip()]
    except ValueError as error:
        raise ValueError("用户 ID 必须是逗号分隔的整数") from error
    if not user_ids:
        raise ValueError("至少需要一个 Telegram 用户 ID")
    token = sys.stdin.read().strip()
    if not token or any(char.isspace() for char in token):
        raise ValueError("Telegram Bot Token 不能为空或包含空白字符")
    token_path = path.parent / token_filename
    atomic_write(token_path, token + "\n")
    previous = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    cooldown = configured_notification_cooldown(
        previous.get("notification_cooldown_seconds", DEFAULT_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS)
        if isinstance(previous, dict) else DEFAULT_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS)
    payload = {"token_file": f"/run/secrets/{token_filename}",
               "allowed_user_ids": sorted(set(user_ids)),
               "notification_cooldown_seconds": cooldown}
    if isinstance(previous, dict) and "online_confirmation_samples" in previous:
        payload["online_confirmation_samples"] = validate_online_confirmation_samples(
            previous["online_confirmation_samples"])
    if isinstance(previous, dict) and "cross_panel_cooldown" in previous:
        payload["cross_panel_cooldown"] = bool(previous["cross_panel_cooldown"])
    atomic_write(path, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def ssh_login_cli(argv):
    parser = argparse.ArgumentParser(description="管理 SSH 登录告警服务器配置")
    parser.add_argument("operation", choices=("list", "status", "set-url", "add", "update", "set-cidrs", "set-groups",
                                               "list-groups", "add-group", "set-group", "rename-group", "delete-group",
                                               "rename", "delete", "install-command"))
    parser.add_argument("--name")
    parser.add_argument("--new-name")
    parser.add_argument("--cidrs", default="")
    parser.add_argument("--groups", default="")
    parser.add_argument("--url")
    parser.add_argument("--format", choices=("display", "tsv"), default="display")
    args = parser.parse_args(argv)
    # Interactive shell input can carry accidental leading/trailing spaces;
    # trim those before validating identifiers while still rejecting spaces
    # inside a server or group name.
    for attribute in ("name", "new_name"):
        value = getattr(args, attribute)
        if value is not None:
            setattr(args, attribute, value.strip())
    path = Path(SSH_LOGIN_CONFIG_FILE)
    config = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {"servers": {}}
    if not isinstance(config, dict) or not isinstance(config.get("servers", {}), dict):
        raise ValueError("SSH 告警配置格式无效")
    servers = config.setdefault("servers", {})
    groups = config.setdefault("ip_groups", {})
    if not isinstance(groups, dict):
        raise ValueError("可信来源 IP 组配置格式无效")
    if args.operation == "status":
        if args.format == "tsv":
            print(f"{len(servers)}\t{1 if config.get('ingest_url') else 0}")
        else:
            print(f"{len(servers)} 台服务器；接收地址：{'已设置' if config.get('ingest_url') else '未设置'}")
        return
    if args.operation == "list":
        if not servers:
            print("尚未添加服务器")
        for name, item in sorted(servers.items()):
            cidrs = item.get("allowed_cidrs", [])
            group_names = item.get("allowed_ip_groups", [])
            trusted_parts = []
            if group_names:
                trusted_parts.append("组=" + ",".join(group_names))
            if cidrs:
                trusted_parts.append("CIDR=" + ",".join(cidrs))
            trusted = "; ".join(trusted_parts) if trusted_parts else "无（所有登录均告警）"
            if args.format == "tsv":
                print(f"{name}\t{trusted}")
            else:
                print(f"{name}\t可信来源={trusted}")
        if args.format == "display" and config.get("ingest_url"):
            print(f"接收地址：{config['ingest_url']}")
        return
    if args.operation == "list-groups":
        for name, cidrs in sorted(groups.items()):
            if args.format == "tsv":
                print(f"{name}\t{', '.join(cidrs)}")
            else:
                assigned = sum(name in item.get("allowed_ip_groups", []) for item in servers.values()
                               if isinstance(item, dict))
                print(f"{name}\t{', '.join(cidrs)}\t已分配服务器={assigned}")
        if not groups and args.format == "display":
            print("尚未添加可信来源 IP 组")
        return
    if args.operation in ("add-group", "set-group"):
        if not args.name or not NAME_RE.fullmatch(args.name):
            raise ValueError("IP 组名称只能使用字母、数字、点、下划线和连字符")
        if args.operation == "add-group" and args.name in groups:
            raise ValueError("IP 组名称已存在")
        if args.operation == "set-group" and args.name not in groups:
            raise ValueError("IP 组不存在")
        cidrs = [value.strip() for value in args.cidrs.split(",") if value.strip()]
        if not cidrs:
            raise ValueError("IP 组至少需要一个可信来源 CIDR")
        for cidr in cidrs:
            ipaddress.ip_network(cidr, strict=False)
        groups[args.name] = cidrs
        atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        return
    if args.operation == "rename-group":
        if not args.name or args.name not in groups:
            raise ValueError("IP 组不存在")
        if not args.new_name or not NAME_RE.fullmatch(args.new_name):
            raise ValueError("新 IP 组名称只能使用字母、数字、点、下划线和连字符")
        if args.new_name != args.name and args.new_name in groups:
            raise ValueError("目标 IP 组名称已存在")
        cidrs = [value.strip() for value in args.cidrs.split(",") if value.strip()]
        if not cidrs:
            raise ValueError("IP 组至少需要一个可信来源 CIDR")
        for cidr in cidrs:
            ipaddress.ip_network(cidr, strict=False)
        del groups[args.name]
        groups[args.new_name] = cidrs
        for item in servers.values():
            if isinstance(item, dict):
                item["allowed_ip_groups"] = sorted({
                    args.new_name if group == args.name else group
                    for group in item.get("allowed_ip_groups", [])
                })
        atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        return
    if args.operation == "delete-group":
        if not args.name or args.name not in groups:
            raise ValueError("IP 组不存在")
        del groups[args.name]
        for item in servers.values():
            if isinstance(item, dict):
                item["allowed_ip_groups"] = [group for group in item.get("allowed_ip_groups", [])
                                             if group != args.name]
        atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        return
    if args.operation == "rename":
        if not args.name or args.name not in servers:
            raise ValueError("服务器不存在")
        if not args.new_name or not NAME_RE.fullmatch(args.new_name):
            raise ValueError("新服务器名称只能使用字母、数字、点、下划线和连字符")
        if args.new_name in servers:
            raise ValueError("该服务器名称已存在")
        servers[args.new_name] = servers.pop(args.name)
        atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")
        return
    if args.operation == "delete":
        if not args.name or args.name not in servers:
            raise ValueError("服务器不存在")
        del servers[args.name]
    elif args.operation == "add":
        if not args.name or not NAME_RE.fullmatch(args.name):
            raise ValueError("服务器名称只能使用字母、数字、点、下划线和连字符")
        if args.name in servers:
            raise ValueError("该服务器名称已存在")
        cidrs = [value.strip() for value in args.cidrs.split(",") if value.strip()]
        for cidr in cidrs:
            ipaddress.ip_network(cidr, strict=False)
        selected_groups = [value.strip() for value in args.groups.split(",") if value.strip()]
        missing = sorted(set(selected_groups) - groups.keys())
        if missing:
            raise ValueError("未找到可信来源 IP 组：" + ", ".join(missing))
        servers[args.name] = {"key": secrets.token_hex(32), "allowed_cidrs": cidrs,
                              "allowed_ip_groups": sorted(set(selected_groups))}
    elif args.operation == "update":
        if not args.name or args.name not in servers:
            raise ValueError("服务器不存在")
        target_name = args.new_name or args.name
        if not NAME_RE.fullmatch(target_name):
            raise ValueError("新服务器名称只能使用字母、数字、点、下划线和连字符")
        if target_name != args.name and target_name in servers:
            raise ValueError("该服务器名称已存在")
        cidrs = [value.strip() for value in args.cidrs.split(",") if value.strip()]
        for cidr in cidrs:
            ipaddress.ip_network(cidr, strict=False)
        selected_groups = [value.strip() for value in args.groups.split(",") if value.strip()]
        missing = sorted(set(selected_groups) - groups.keys())
        if missing:
            raise ValueError("未找到可信来源 IP 组：" + ", ".join(missing))
        entry = servers.pop(args.name)
        entry["allowed_cidrs"] = cidrs
        entry["allowed_ip_groups"] = sorted(set(selected_groups))
        servers[target_name] = entry
    elif args.operation == "set-cidrs":
        if not args.name or args.name not in servers:
            raise ValueError("服务器不存在")
        cidrs = [value.strip() for value in args.cidrs.split(",") if value.strip()]
        for cidr in cidrs:
            ipaddress.ip_network(cidr, strict=False)
        servers[args.name]["allowed_cidrs"] = cidrs
    elif args.operation == "set-groups":
        if not args.name or args.name not in servers:
            raise ValueError("服务器不存在")
        selected_groups = [value.strip() for value in args.groups.split(",") if value.strip()]
        missing = sorted(set(selected_groups) - groups.keys())
        if missing:
            raise ValueError("未找到可信来源 IP 组：" + ", ".join(missing))
        servers[args.name]["allowed_ip_groups"] = sorted(set(selected_groups))
        servers[args.name]["allowed_cidrs"] = []
    elif args.operation == "install-command":
        if not args.name or args.name not in servers:
            raise ValueError("服务器不存在")
        url = args.url or config.get("ingest_url", "")
        if not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/api/ssh-login", url):
            raise ValueError("请先设置有效的 HTTPS 接收地址")
        key = servers[args.name]["key"]
        print(f"SERVER_NAME={shlex.quote(args.name)} SSH_LOGIN_URL={shlex.quote(url)} "
              f"SSH_LOGIN_KEY={shlex.quote(key)} bash <(curl -fsSL "
              "https://raw.githubusercontent.com/xhpx7301/gla/main/install-ssh-login-agent.sh)")
        return
    elif args.operation == "set-url":
        if not args.url or not re.fullmatch(r"https://[A-Za-z0-9.-]+(?::[0-9]+)?/api/ssh-login", args.url):
            raise ValueError("接收地址必须是 HTTPS URL，并以 /api/ssh-login 结尾")
        config["ingest_url"] = args.url
    atomic_write(path, json.dumps(config, ensure_ascii=False, indent=2) + "\n")


def admin_overview_payload(admin_config=None):
    worker_map = {worker.panel["name"]: worker for worker in WORKERS}
    panels_list = []
    if PANELS_FILE and Path(PANELS_FILE).is_file():
        for panel in load_config(PANELS_FILE):
            worker = worker_map.get(panel["name"])
            with (worker.lock if worker else threading.Lock()):
                up = bool(worker.up) if worker else False
                last_error = worker.last_error if worker else "未运行"
                last_success = worker.last_success if worker else 0.0
            panels_list.append({
                "name": panel["name"],
                "url": panel["url"],
                "logs": bool(panel.get("logs", False)),
                "interval": panel.get("interval", 30),
                "log_count": panel.get("log_count", 1000),
                "up": up,
                "last_error": last_error,
                "last_success": last_success,
            })
    ssh_raw = json.loads(Path(SSH_LOGIN_CONFIG_FILE).read_text(encoding="utf-8")) if Path(SSH_LOGIN_CONFIG_FILE).is_file() else {}
    ssh_servers = {}
    for name, info in sorted((ssh_raw.get("servers") or {}).items()):
        if isinstance(info, dict):
            ssh_servers[name] = {
                "allowed_cidrs": info.get("allowed_cidrs", []),
                "allowed_ip_groups": info.get("allowed_ip_groups", []),
            }
    tg_raw = json.loads(Path(TELEGRAM_CONFIG_FILE).read_text(encoding="utf-8")) if Path(TELEGRAM_CONFIG_FILE).is_file() else {}
    ssh_tg_raw = json.loads(Path(SSH_TELEGRAM_CONFIG_FILE).read_text(encoding="utf-8")) if Path(SSH_TELEGRAM_CONFIG_FILE).is_file() else {}
    return {
        "server_name": SERVER_NAME,
        "admin": {
            "username": (admin_config or {}).get("username", DEFAULT_WEB_ADMIN_USERNAME),
            "default_password": bool((admin_config or {}).get("default_password", False)),
        },
        "panels": panels_list,
        "ssh_login": {
            "ingest_url": ssh_raw.get("ingest_url", ""),
            "ip_groups": ssh_raw.get("ip_groups", {}),
            "servers": ssh_servers,
        },
        "telegram": {
            "configured": Path(TELEGRAM_CONFIG_FILE).is_file(),
            "allowed_user_ids": tg_raw.get("allowed_user_ids", []),
            "notification_cooldown_seconds": configured_notification_cooldown(
                tg_raw.get("notification_cooldown_seconds", DEFAULT_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS)),
            "online_confirmation_samples": validate_online_confirmation_samples(
                tg_raw.get("online_confirmation_samples", TELEGRAM_ONLINE_CONFIRMATION_SAMPLES)),
            "cross_panel_cooldown": bool(tg_raw.get("cross_panel_cooldown", False)),
        },
        "ssh_telegram": {
            "configured": Path(SSH_TELEGRAM_CONFIG_FILE).is_file(),
            "allowed_user_ids": ssh_tg_raw.get("allowed_user_ids", []),
        },
    }


def _run_cli_with_stdin(func, argv, stdin_text=""):
    old_stdin = sys.stdin
    try:
        sys.stdin = io.StringIO(stdin_text)
        return func(argv)
    finally:
        sys.stdin = old_stdin


def apply_admin_action(method, path, body, admin_config):
    import io
    if path == "/api/admin/panels":
        if method == "DELETE":
            panels_cli(["delete", "--name", str(body.get("name", ""))])
        else:
            op = "update" if body.get("operation") == "update" else "add"
            argv = [op, "--name", str(body.get("name", "")),
                    "--url", str(body.get("url", "")),
                    "--logs", "true" if body.get("logs", True) else "false",
                    "--interval", str(int(body.get("interval", 30))),
                    "--log-count", str(int(body.get("log_count", 1000)))]
            new_name = str(body.get("new_name", "")).strip()
            if op == "update" and new_name:
                argv.extend(["--new-name", new_name])
            old_stdin = sys.stdin
            try:
                sys.stdin = io.StringIO(str(body.get("token", "")))
                panels_cli(argv)
            finally:
                sys.stdin = old_stdin
        if RUNTIME is not None:
            RUNTIME.reload_panels()
        return {"ok": True}

    if path == "/api/admin/ssh-login/url" and method == "POST":
        ssh_login_cli(["set-url", "--url", str(body.get("url", ""))])
        return {"ok": True}

    if path == "/api/admin/ssh-login/groups":
        name = str(body.get("name", "")).strip()
        orig_name = str(body.get("orig_name", "")).strip()
        if method == "DELETE":
            ssh_login_cli(["delete-group", "--name", name])
        elif orig_name and orig_name != name:
            ssh_login_cli(["rename-group", "--name", orig_name, "--new-name", name, "--cidrs", str(body.get("cidrs", ""))])
        else:
            existing = json.loads(Path(SSH_LOGIN_CONFIG_FILE).read_text(encoding="utf-8")).get("ip_groups", {}) if Path(SSH_LOGIN_CONFIG_FILE).is_file() else {}
            op = "set-group" if name in existing else "add-group"
            ssh_login_cli([op, "--name", name, "--cidrs", str(body.get("cidrs", ""))])
        return {"ok": True}

    if path == "/api/admin/ssh-login/servers":
        name = str(body.get("name", "")).strip()
        orig_name = str(body.get("orig_name", "")).strip()
        if method == "DELETE":
            ssh_login_cli(["delete", "--name", name])
        elif orig_name:
            ssh_login_cli(["update", "--name", orig_name, "--new-name", name,
                           "--cidrs", str(body.get("cidrs", "")), "--groups", str(body.get("groups", ""))])
        else:
            ssh_login_cli(["add", "--name", name, "--cidrs", str(body.get("cidrs", "")), "--groups", str(body.get("groups", ""))])
        return {"ok": True}

    if path == "/api/admin/ssh-login/install-command" and method == "POST":
        buf = io.StringIO()
        old_stdout = sys.stdout
        try:
            sys.stdout = buf
            ssh_login_cli(["install-command", "--name", str(body.get("name", ""))])
        finally:
            sys.stdout = old_stdout
        return {"command": buf.getvalue().strip()}

    if path == "/api/admin/telegram" and method == "POST":
        tg_path = Path(TELEGRAM_CONFIG_FILE)
        token = str(body.get("token", "")).strip()
        if not token:
            token = read_token(tg_path.parent / "telegram-bot.token")
        cooldown = validate_notification_cooldown(int(body.get("notification_cooldown_seconds", DEFAULT_TELEGRAM_NOTIFICATION_COOLDOWN_SECONDS)))
        confirm = validate_online_confirmation_samples(int(body.get("online_confirmation_samples", TELEGRAM_ONLINE_CONFIRMATION_SAMPLES)))
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(token)
            telegram_cli(["set", "--user-ids", str(body.get("user_ids", ""))])
        finally:
            sys.stdin = old_stdin
        saved = json.loads(tg_path.read_text(encoding="utf-8"))
        saved["notification_cooldown_seconds"] = cooldown
        saved["online_confirmation_samples"] = confirm
        saved["cross_panel_cooldown"] = bool(body.get("cross_panel_cooldown", False))
        atomic_write(tg_path, json.dumps(saved, ensure_ascii=False, indent=2) + "\n")
        if RUNTIME is not None:
            RUNTIME.reload_telegram()
        return {"ok": True}

    if path == "/api/admin/telegram/test" and method == "POST":
        telegram_cli(["test"])
        return {"ok": True}

    if path == "/api/admin/ssh-login/telegram" and method == "POST":
        tg_path = Path(SSH_TELEGRAM_CONFIG_FILE)
        token = str(body.get("token", "")).strip()
        if not token:
            token = read_token(tg_path.parent / "ssh-telegram-bot.token")
        old_stdin = sys.stdin
        try:
            sys.stdin = io.StringIO(token)
            telegram_cli(["set", "--user-ids", str(body.get("user_ids", ""))],
                         SSH_TELEGRAM_CONFIG_FILE, "ssh-telegram-bot.token")
        finally:
            sys.stdin = old_stdin
        return {"ok": True}

    if path == "/api/admin/ssh-login/telegram/test" and method == "POST":
        telegram_cli(["test"], SSH_TELEGRAM_CONFIG_FILE, "ssh-telegram-bot.token",
                     "✅ SSH 登录告警 Bot 测试消息；接收配置正常。")
        return {"ok": True}

    if path == "/api/admin/password" and method == "POST":
        if not verify_admin_password(str(body.get("current_password", "")), admin_config):
            raise ValueError("当前密码错误")
        new_user = str(body.get("new_username", "")).strip() or None
        save_web_admin_password(str(body.get("new_password", "")),
                                session_secret=admin_config["session_secret"],
                                username=new_user, default_password=False)
        return {"ok": True}

    raise ValueError("未知操作路径")


def main():
    global RUNTIME
    if len(sys.argv) > 1 and sys.argv[1] == "panels":
        panels_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "telegram":
        telegram_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "ssh-telegram":
        telegram_cli(sys.argv[2:], SSH_TELEGRAM_CONFIG_FILE, "ssh-telegram-bot.token",
                     "✅ SSH 登录告警 Bot 测试消息；接收配置正常。")
        return
    if len(sys.argv) > 1 and sys.argv[1] == "ssh-login":
        ssh_login_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "web-admin":
        web_admin_cli(sys.argv[2:])
        return
    stop_event = threading.Event()
    RUNTIME = RuntimeManager(stop_event)
    if PANELS_FILE:
        RUNTIME.reload_panels()
        RUNTIME.reload_telegram()
    elif not API_URL:
        raise ValueError("缺少 XUI_API_URL 或 XUI_PANELS_FILE")
    server = ThreadingHTTPServer(("0.0.0.0", 9105), MetricsHandler)

    def handle_shutdown(_signum, _frame):
        if not stop_event.is_set():
            stop_event.set()
            threading.Thread(target=server.shutdown, daemon=True, name="shutdown").start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, handle_shutdown)
    print("xui exporter started", flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()
        RUNTIME.stop_all()


if __name__ == "__main__":
    try:
        main()
    except ERRORS as error:
        if len(sys.argv) > 1 and sys.argv[1] == "ssh-login" and isinstance(error, ValueError):
            print(f"启动/配置失败: {error}", file=sys.stderr)
        else:
            print(f"启动/配置失败: {error_kind(error)}", file=sys.stderr)
        raise SystemExit(1)
