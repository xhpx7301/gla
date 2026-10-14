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
import urllib.parse
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


API_URL = os.environ.get("XUI_API_URL", "").rstrip("/")
TOKEN_FILE = os.environ.get("XUI_API_TOKEN_FILE", "/run/secrets/xui_api_token")
PANELS_FILE = os.environ.get("XUI_PANELS_FILE", "")
STATE_DIR = os.environ.get("XUI_STATE_DIR", "/var/lib/xui")
LOKI_URL = os.environ.get("XUI_LOKI_URL", "http://alloy:3500/loki/api/v1/push")
TELEGRAM_CONFIG_FILE = os.environ.get("XUI_TELEGRAM_CONFIG_FILE", "/run/secrets/telegram-config.json")
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
    return {**config, "allowed_user_ids": user_ids, "allowed_chat_ids": chat_ids, "token": read_token(token_file)}


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


class TelegramBot:
    BUTTON_COMMANDS = {
        "📡 状态": "/status", "👥 分组": "/groups", "🟢 在线客户端": "/online",
        "📈 累计流量": "/traffic", "🔔 添加订阅": "/watch",
        "🔕 取消订阅": "/unwatch", "📋 我的订阅": "/watches", "❔ 帮助": "/help",
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
        {"command": "watch", "description": "订阅分组上线通知"},
        {"command": "unwatch", "description": "取消分组上线通知"},
        {"command": "watches", "description": "查看已订阅分组"},
    ]

    def __init__(self, config, workers, state_dir):
        self.config = config
        self.workers = workers
        Path(state_dir).mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(Path(state_dir) / "telegram.sqlite"), timeout=10, check_same_thread=False)
        self.db.execute("CREATE TABLE IF NOT EXISTS state (server TEXT, email TEXT, group_name TEXT, online INTEGER, PRIMARY KEY(server,email))")
        self.db.execute("CREATE TABLE IF NOT EXISTS subscriptions (chat_id INTEGER, group_name TEXT, PRIMARY KEY(chat_id,group_name))")
        self.db.execute("CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT)")
        self.offset = 0
        self.pending_groups = {}

    def snapshot(self):
        series = {}
        servers_up = set()
        for worker in self.workers:
            for line in worker.snapshot():
                parsed = parse_metric_line(line)
                if not parsed:
                    continue
                name, tags, value = parsed
                server = tags.get("server", "")
                if name == "xui_exporter_up" and value == 1:
                    servers_up.add(server)
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
        return series, servers_up

    @staticmethod
    def main_keyboard():
        return {"inline_keyboard": [
            [{"text": "📡 状态", "callback_data": "menu:status"},
             {"text": "👥 分组", "callback_data": "menu:groups"}],
            [{"text": "🟢 在线客户端", "callback_data": "menu:online"},
             {"text": "📈 累计流量", "callback_data": "menu:traffic"}],
            [{"text": "🔔 添加订阅", "callback_data": "menu:watch"},
             {"text": "🔕 取消订阅", "callback_data": "menu:unwatch"}],
            [{"text": "📋 我的订阅", "callback_data": "menu:watches"},
             {"text": "❔ 帮助", "callback_data": "menu:help"}],
            [{"text": "📖 命令说明", "callback_data": "menu:commands"}],
        ]}

    def send(self, chat_id, text, reply_markup=None):
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
        for index, chunk in enumerate(chunks):
            payload = {"chat_id": chat_id, "text": chunk}
            if reply_markup is not None and index == len(chunks) - 1:
                payload["reply_markup"] = json.dumps(reply_markup, ensure_ascii=False, separators=(",", ":"))
            telegram_request(self.config["token"], "sendMessage", payload)

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
        telegram_request(self.config["token"], "setMyCommands", {
            "commands": json.dumps(self.COMMANDS, ensure_ascii=False, separators=(",", ":"))
        })

    def process_online(self):
        series, servers_up = self.snapshot()
        if not servers_up:
            return
        baseline = self.db.execute("SELECT value FROM metadata WHERE key='baseline'").fetchone()
        changes = []
        with self.db:
            current_keys = set(series)
            for old_server, old_email in self.db.execute("SELECT server,email FROM state").fetchall():
                if old_server in servers_up and (old_server, old_email) not in current_keys:
                    self.db.execute("UPDATE state SET online=0 WHERE server=? AND email=?", (old_server, old_email))
            for key, item in series.items():
                if item["server"] not in servers_up:
                    continue
                old = self.db.execute("SELECT group_name,online FROM state WHERE server=? AND email=?", key).fetchone()
                if old and old[1] == 0 and item["online"] == 1:
                    changes.append((key[0], item["email"], item["group"], "online"))
                self.db.execute("INSERT OR REPLACE INTO state VALUES (?,?,?,?)", (*key, item["group"], item["online"]))
            if baseline is None:
                self.db.execute("INSERT INTO metadata VALUES ('baseline','1')")
                changes.clear()
        for server, email, group, _ in changes:
            chats = [row[0] for row in self.db.execute("SELECT chat_id FROM subscriptions WHERE group_name=?", (group,))]
            for chat_id in chats:
                try:
                    self.send(chat_id, f"🟢 客户端上线\n\n👤 客户端：{email}\n👥 分组：{group}\n🖥 面板：{server}\n\n🕒 检测时间：{self.report_time()}")
                except ERRORS as error:
                    print(f"Telegram 通知发送失败 ({error_kind(error)})", file=sys.stderr, flush=True)

    def handle_message(self, message):
        chat_id = message.get("chat", {}).get("id")
        user_id = message.get("from", {}).get("id")
        allowed_users = set(self.config.get("allowed_user_ids", []))
        allowed_chats = set(self.config.get("allowed_chat_ids", []))
        if not isinstance(chat_id, int):
            return
        text = (message.get("text") or "").strip()
        # Old reply keyboards send the entire label, including spaces.
        # Resolve it before parsing slash command arguments.
        legacy_command = {label.replace("\ufe0f", ""): command
                          for label, command in self.BUTTON_COMMANDS.items()}.get(text.replace("\ufe0f", ""))
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
        series, servers_up = self.snapshot()
        groups = sorted({item["group"] for item in series.values()})
        if command == "/start":
            name = message.get("from", {}).get("first_name") or "朋友"
            self.send(chat_id, f"👋 你好，{name}！\n🤖 欢迎使用 GLA 分组监控机器人。", {"remove_keyboard": True})
            self.send(chat_id, "👇 请选择：", self.main_keyboard())
            return
        elif command == "/help":
            reply = ("🤖 欢迎使用 GLA 分组监控机器人！\n"
                     "您可以查询各面板的状态、分组在线客户端和累计流量，"
                     "也可以订阅指定分组的客户端上线通知。\n\n"
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
                     "/watch [分组] - 订阅上线通知\n"
                     "/unwatch [分组] - 取消订阅\n"
                     "/watches - 查看已订阅分组\n"
                     "/id - 显示您的 Telegram ID\n\n"
                     "[分组] 可省略；订阅和取消订阅时会显示分组选择按钮。\n"
                     "示例：/online Aurora 极光云瓶\n"
                     "流量为面板返回的累计值。")
            self.send(chat_id, reply, self.main_keyboard())
            return
        elif command == "/status":
            series, servers_up = self.snapshot()
            panel_names = [worker.panel["name"] for worker in self.workers]
            online_count = sum(1 for item in series.values() if item["online"] and item["server"] in servers_up)
            panel_lines = [f"{'🟢' if name in servers_up else '🔴'} {name}：{'正常' if name in servers_up else '不可用'}" for name in panel_names]
            reply = ("📡 运行状态\n\n🤖 机器人运行正常\n"
                     f"🖥 面板：{len(servers_up)}/{len(panel_names)} 正常\n"
                     f"👥 在线分组：{len({item['group'] for item in series.values() if item['online']})}\n"
                     f"🟢 在线客户端：{online_count}\n\n" + ("\n".join(panel_lines) if panel_lines else "尚未配置面板")
                     + "\n\n🕒 查询时间：" + self.report_time())
        elif command == "/groups":
            blocks = []
            for group in groups:
                members = [item for item in series.values() if item["group"] == group]
                blocks.append(f"👥 分组：{group}\n🟢 在线：{sum(item['online'] for item in members)}/{len(members)} 个客户端")
            reply = "👥 客户端分组\n\n" + ("\n\n".join(blocks) if blocks else "📭 暂无分组数据") + self.report_footer(servers_up)
        elif command == "/online":
            rows = [item for item in series.values() if item["online"] and (not argument or item["group"] == argument)]
            blocks = [f"👤 客户端：{item['email']}\n👥 分组：{item['group']}\n🖥 面板：{item['server']}\n"
                      + self.traffic_details(item['up'], item['down'])
                      for item in sorted(rows, key=lambda item: (item['group'], item['server'], item['email']))]
            reply = (f"🟢 在线客户端：{len(rows)} 个\n👥 筛选分组：{argument or '全部'}\n\n"
                     + ("\n\n".join(blocks) if blocks else "📭 没有匹配的在线客户端")
                     + "\n\nℹ️ 流量为客户端累计用量。" + self.report_footer(servers_up))
        elif command == "/traffic":
            totals = {}
            for item in series.values():
                if argument and item["group"] != argument:
                    continue
                total = totals.setdefault(item["group"], [0, 0])
                total[0] += item["up"]; total[1] += item["down"]
            rows = [f"👥 分组：{group}\n" + self.traffic_details(values[0], values[1])
                    for group, values in sorted(totals.items(), key=lambda row: (-sum(row[1]), row[0]))]
            reply = ("📈 分组累计流量\n\n" + ("\n\n".join(rows) if rows else "📭 没有匹配的分组流量")
                     + "\n\nℹ️ 同名分组跨面板汇总，按累计总量排序。" + self.report_footer(servers_up))
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
            if argument not in groups:
                reply = f"没有找到分组：{argument}。使用 /groups 查看分组。"
            elif command == "/watch":
                with self.db: self.db.execute("INSERT OR IGNORE INTO subscriptions VALUES (?,?)", (chat_id, argument))
                reply = f"🔔 已订阅分组：{argument}"
            else:
                with self.db: self.db.execute("DELETE FROM subscriptions WHERE chat_id=? AND group_name=?", (chat_id, argument))
                reply = f"🔕 已取消订阅：{argument}"
        elif command == "/watches":
            rows = [row[0] for row in self.db.execute("SELECT group_name FROM subscriptions WHERE chat_id=? ORDER BY group_name", (chat_id,))]
            reply = "📋 已订阅分组\n\n" + ("\n".join(f"🔔 {group}" for group in rows) if rows else "📭 暂无订阅")
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
        menu_commands = {"menu:status": "/status", "menu:groups": "/groups",
                         "menu:online": "/online", "menu:traffic": "/traffic",
                         "menu:watch": "/watch", "menu:unwatch": "/unwatch",
                         "menu:watches": "/watches", "menu:help": "/help",
                         "menu:commands": "/commands"}
        data = callback.get("data", "")
        if data in menu_commands:
            telegram_request(self.config["token"], "answerCallbackQuery", {"callback_query_id": callback_id})
            self.handle_message({"chat": {"id": chat_id}, "from": {"id": user_id}, "text": menu_commands[data]})
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

    def run(self):
        try:
            self.register_commands()
            print("Telegram command menu registered", flush=True)
        except ERRORS as error:
            print(f"Telegram 命令菜单注册失败 ({error_kind(error)})", file=sys.stderr, flush=True)
        next_check = 0
        while True:
            if time.monotonic() >= next_check:
                self.process_online()
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
                time.sleep(5)


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
        self.client_groups = {}
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
            self.client_groups = client_group_map(clients or [])
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
            stats = forwarder.forward(entries, client_groups=self.client_groups)
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


def telegram_cli(argv):
    parser = argparse.ArgumentParser(description="管理 Telegram Bot 配置")
    parser.add_argument("operation", choices=("set", "delete", "show"))
    parser.add_argument("--user-ids", default="")
    args = parser.parse_args(argv)
    path = Path(TELEGRAM_CONFIG_FILE)
    if args.operation == "show":
        config = telegram_config()
        if config:
            print("已配置 Telegram Bot；允许用户数：%d" % len(config["allowed_user_ids"]))
        else:
            print("未配置 Telegram Bot")
        return
    if args.operation == "delete":
        path.unlink(missing_ok=True)
        path.parent.joinpath("telegram-bot.token").unlink(missing_ok=True)
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
    token_path = path.parent / "telegram-bot.token"
    atomic_write(token_path, token + "\n")
    atomic_write(path, json.dumps({"token_file": "/run/secrets/telegram-bot.token",
                                   "allowed_user_ids": sorted(set(user_ids))}, ensure_ascii=False, indent=2) + "\n")


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "panels":
        panels_cli(sys.argv[2:])
        return
    if len(sys.argv) > 1 and sys.argv[1] == "telegram":
        telegram_cli(sys.argv[2:])
        return
    if PANELS_FILE:
        panels = load_config(PANELS_FILE)
        for panel in panels:
            worker = PanelWorker(panel)
            WORKERS.append(worker)
            threading.Thread(target=worker.run, daemon=True, name=panel["name"]).start()
        try:
            config = telegram_config()
            if config:
                threading.Thread(target=TelegramBot(config, WORKERS, STATE_DIR).run,
                                 daemon=True, name="telegram-bot").start()
                print("Telegram Bot started", flush=True)
        except ERRORS as error:
            print(f"Telegram 配置无效，Bot 未启动 ({error_kind(error)})", file=sys.stderr, flush=True)
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
