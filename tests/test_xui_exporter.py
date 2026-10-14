import importlib.util
import io
import json
import pathlib
import sys
import tempfile
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).parents[1] / "assets" / "xui_exporter.py"
SPEC = importlib.util.spec_from_file_location("xui_exporter", MODULE_PATH)
EXPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORTER)


class ExporterTest(unittest.TestCase):
    def test_client_groups_are_scoped_per_panel_and_escape_metric_labels(self):
        clients_a = [{"email": "alice", "group": "shared", "traffic": {"up": 1}},
                     {"email": "same-email", "group": 'A"组\\线路', "traffic": {}},
                     {"email": "ungrouped", "group": "", "traffic": {}}]
        clients_b = [{"email": "bob", "group": "shared", "traffic": {"up": 2}},
                     {"email": "same-email", "group": "B组", "traffic": {}}]
        a = EXPORTER.render_metrics("panel-a", [], ["alice"], clients_a)
        b = EXPORTER.render_metrics("panel-b", [], ["bob"], clients_b)
        self.assertIn('xui_client_online{server="panel-a",email="alice",group="shared"} 1', a)
        self.assertIn('xui_client_online{server="panel-b",email="bob",group="shared"} 1', b)
        self.assertIn('group="A\\"组\\\\线路"', "\n".join(a))
        self.assertNotIn('group="B组"', "\n".join(a))
        self.assertIn('group="未分组"', "\n".join(a))
        self.assertEqual(EXPORTER.client_group_map(clients_b)["same-email"], "B组")

    def test_inbound_client_map_reads_json_settings_and_client_stats(self):
        inbounds = [
            {"id": 1, "remark": "VLESS-main", "port": 443, "protocol": "vless",
             "settings": json.dumps({"clients": [{"email": "alice"}, {"email": "shared"}]})},
            {"id": 2, "tag": "trojan-backup", "port": 8443, "protocol": "trojan",
             "settings": {"clients": [{"email": "shared"}]},
             "clientStats": [{"email": "bob"}]},
        ]
        self.assertEqual(EXPORTER.inbound_client_map(inbounds), {
            "alice": ["VLESS-main"],
            "bob": ["trojan-backup"],
            "shared": ["VLESS-main", "trojan-backup"],
        })

    def test_worker_associates_online_notification_with_configured_inbounds(self):
        panel = EXPORTER.PanelWorker({"name": "node-a", "url": "https://panel.example.com/panel/api/inbounds/list",
                                      "token_file": "/run/secrets/xui-node-a.token", "logs": False})
        inbound = {"id": 7, "remark": "VLESS-443", "port": 443, "protocol": "vless",
                   "settings": json.dumps({"clients": [{"email": "alice"}]})}
        clients = [{"email": "alice", "group": "shared", "traffic": {}}]
        def api(url, method="GET", token=None, form=None):
            if url.endswith("/clients/list"):
                return clients
            if url.endswith("/clients/onlines"):
                return []
            return [inbound]
        with mock.patch.object(EXPORTER, "read_token", return_value="secret"), \
                mock.patch.object(EXPORTER, "request_json", side_effect=api):
            panel.poll()
        self.assertEqual(panel.inbounds_for_client("alice"), ["VLESS-443"])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as send, \
                mock.patch.object(EXPORTER.TelegramBot, "report_time", return_value="test-time"), \
                mock.patch.object(panel, "last_ip_for_client", return_value={
                    "ip": "203.0.113.1", "time": "2026-10-14 16:30:00（UTC+8）"}), \
                mock.patch.object(EXPORTER, "ip_attribution", return_value=("中国深圳", "中国电信")):
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42],
                                        "notification_cooldown_seconds": 1800}, [panel], directory)
            try:
                with bot.db:
                    bot.db.execute("INSERT INTO subscriptions VALUES (?,?)", (420, "shared"))
                bot.process_online()  # baseline is quiet
                panel.metrics = EXPORTER.render_metrics("node-a", [inbound], [], clients)
                bot.process_online()  # offline sample
                with mock.patch.object(EXPORTER.time, "time", return_value=1060):
                    panel.metrics = EXPORTER.render_metrics("node-a", [inbound], ["alice"], clients)
                bot.process_online()
                send.assert_not_called()
                with mock.patch.object(EXPORTER.time, "time", return_value=1090):
                    panel.metrics = EXPORTER.render_metrics("node-a", [inbound], ["alice"], clients)
                bot.process_online()
                send.assert_not_called()
                for timestamp in (1120, 1150, 1180):
                    with mock.patch.object(EXPORTER.time, "time", return_value=timestamp):
                        panel.metrics = EXPORTER.render_metrics("node-a", [inbound], ["alice"], clients)
                    bot.process_online()
                message = send.call_args.args[2]["text"]
                self.assertIn("👤 客户端：alice\n👥 分组：shared\n🔗 关联入站：\n  • VLESS-443\n🖥 面板：node-a", message)
                self.assertNotIn("端口 443", message)
                self.assertNotIn("/ vless", message)
                self.assertIn("🌐 最近记录 IP：203.0.113.1", message)
                self.assertIn("📍 归属地：中国深圳\n🏢 网络组织：中国电信", message)
                self.assertIn("🕒 IP 记录时间：2026-10-14 16:30:00（UTC+8）", message)
            finally:
                bot.db.close()

    def test_ip_log_formats_from_supported_panel_versions_are_normalized(self):
        self.assertEqual(EXPORTER.latest_ip_record(["203.0.113.1 (2026-10-14 08:30:00)"]),
                         {"ip": "203.0.113.1", "time": "2026-10-14 16:30:00（UTC+8）"})
        self.assertEqual(EXPORTER.latest_ip_record([
            {"ip": "2001:4860:4860::8888", "time": "2026-10-14T08:30:00Z", "node": "node-a"}]),
            {"ip": "2001:4860:4860::8888", "time": "2026-10-14 16:30:00（UTC+8）"})
        self.assertEqual(EXPORTER.latest_ip_record([
            {"ip": "203.0.113.2", "time": "2026-10-14T08:30:00-04:00"}]),
            {"ip": "203.0.113.2", "time": "2026-10-14 20:30:00（UTC+8）"})
        self.assertIsNone(EXPORTER.latest_ip_record(["no valid IP"] ))

    def test_ip_attribution_uses_local_city_and_asn_records(self):
        def lookup(path, address):
            if path == EXPORTER.GEOIP_CITY_DB:
                return {"country": {"iso_code": "CN"}, "city": {"names": {"zh-CN": "深圳"}}}
            return {"autonomous_system_number": 4134, "autonomous_system_organization": "CHINANET"}
        with mock.patch.object(EXPORTER, "mmdb_record", side_effect=lookup):
            self.assertEqual(EXPORTER.ip_attribution("203.0.113.1"), ("中国深圳", "中国电信"))
        with mock.patch.object(EXPORTER, "mmdb_record", side_effect=[
                {}, {"autonomous_system_number": 4837, "autonomous_system_organization": "CHINA169"}]):
            self.assertEqual(EXPORTER.ip_attribution("203.0.113.1"), ("中国大陆", "中国联通"))
        with mock.patch.object(EXPORTER, "mmdb_record", side_effect=[
                {"country": {"iso_code": "US", "names": {"en": "United States"}},
                 "city": {"names": {"en": "Los Angeles"}}},
                {"autonomous_system_organization": "Example Networks"}]):
            self.assertEqual(EXPORTER.ip_attribution("203.0.113.1"), ("美国·Los Angeles", "Example Networks"))
        with mock.patch.object(EXPORTER, "mmdb_record", side_effect=[
                {"country": {"iso_code": "US", "names": {"en": "United States"}}},
                {"autonomous_system_organization": "Alibaba (US) Technology Co., Ltd."}]) as lookup:
            location, network = EXPORTER.ip_attribution("203.0.113.1")
            self.assertEqual(location, "美国")
            self.assertEqual(network, "Alibaba (US) Technology Co., Ltd.")
            self.assertEqual(lookup.call_count, 2)

    def test_worker_fetches_latest_client_ip_from_panel_api(self):
        worker = EXPORTER.PanelWorker({"name": "node-a", "url": "https://panel.example.com/panel/api/inbounds/list",
                                       "token_file": "/run/secrets/xui-node-a.token"})
        with mock.patch.object(EXPORTER, "read_token", return_value="secret"), \
                mock.patch.object(EXPORTER, "request_json", return_value=[
                    {"ip": "198.51.100.4", "time": "2026-10-14T10:00:00Z"}]) as request:
            self.assertEqual(worker.last_ip_for_client("alice@example.com"),
                             {"ip": "198.51.100.4", "time": "2026-10-14 18:00:00（UTC+8）"})
        self.assertTrue(request.call_args.args[0].endswith("/clients/ips/alice%40example.com"))
        self.assertEqual(request.call_args.args[1], "POST")

    def test_log_groups_and_group_changes_do_not_replay_seen_records(self):
        panel = {"name": "panel-a", "url": "https://panel.example.com/panel/api/inbounds/list"}
        entry = {"DateTime": "2026-10-14T00:00:00Z", "FromAddress": "1.2.3.4:1234",
                 "ToAddress": "tcp:example.com:443", "Email": "alice"}
        self.assertEqual(EXPORTER.normalize_log(entry, "panel-a", {"alice": "shared"})[2]["group"], "shared")
        self.assertEqual(EXPORTER.normalize_log(entry, "panel-b", {"alice": "other"})[2]["group"], "other")
        self.assertEqual(EXPORTER.normalize_log(entry, "panel-a", {"alice": "未分组"})[2]["group"], "未分组")
        self.assertEqual(EXPORTER.normalize_log(dict(entry, Email=""), "panel-a", {"alice": "shared"})[2]["group"], "未识别客户端")
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "push_loki") as push:
            forwarder = EXPORTER.LogForwarder(panel, directory)
            try:
                forwarder.forward([entry], now=1791936000, client_groups={"alice": "shared"})
                self.assertEqual(push.call_args[0][0][0]["stream"]["group"], "shared")
                forwarder.forward([entry], now=1791936030, client_groups={"alice": "renamed"})
                push.assert_called_once()
            finally:
                forwarder.close()

    def test_worker_retains_panel_group_map_when_client_api_fails(self):
        panel = {"name": "node-a", "url": "https://panel.example.com/panel/api/inbounds/list",
                 "token_file": "/run/secrets/xui-node-a.token", "logs": True}
        worker = EXPORTER.PanelWorker(panel)
        forwarder = mock.Mock()
        forwarder.forward.return_value = {"window_gap": 0}
        fail_clients = False
        def request(url, method="GET", token=None, form=None):
            if url.endswith("/clients/list"):
                if fail_clients:
                    raise OSError("unavailable")
                return [{"email": "alice", "group": "shared", "traffic": {}}]
            return []
        with mock.patch.object(EXPORTER, "read_token", return_value="secret"), \
                mock.patch.object(EXPORTER, "request_json", side_effect=request):
            worker.poll(forwarder)
            self.assertIn('group="shared"', "\n".join(worker.snapshot()))
            fail_clients = True
            worker.poll(forwarder)
            forwarder.forward.assert_called_with([], client_groups={"alice": "shared"})

    def test_update_retains_unprovided_fields_token_and_list_order(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "panels.json"
            with mock.patch.object(EXPORTER, "PANELS_FILE", str(path)):
                for name in ("first", "second"):
                    with mock.patch("sys.stdin", io.StringIO(name + "-token")):
                        EXPORTER.panels_cli(["add", "--name", name, "--url",
                            "https://panel.example.com/panel/api/inbounds/list",
                            "--logs", "false", "--interval", "77", "--log-count", "4321"])
                panels = EXPORTER.load_config(path)
                panels[1]["max_log_age"] = 7200
                path.write_text(json.dumps(panels), encoding="utf-8")
                with mock.patch("sys.stdin", io.StringIO("")):
                    EXPORTER.panels_cli(["update", "--name", "second"])
                    self.assertEqual(EXPORTER.load_config(path), panels)
                    EXPORTER.panels_cli(["update", "--name", "second", "--new-name", "renamed",
                                        "--logs", "true"])
                updated = EXPORTER.load_config(path)
                self.assertEqual([p["name"] for p in updated], ["first", "renamed"])
                self.assertTrue(updated[1]["logs"])
                self.assertEqual(updated[1]["interval"], 77)
                self.assertEqual(updated[1]["max_log_age"], 7200)
                self.assertEqual((path.parent / "xui-renamed.token").read_text().strip(), "second-token")
                self.assertFalse((path.parent / "xui-second.token").exists())
                saved = path.read_bytes()
                with mock.patch("sys.stdin", io.StringIO("replacement")):
                    with self.assertRaises(ValueError):
                        EXPORTER.panels_cli(["add", "--name", "first"])
                    with self.assertRaises(ValueError):
                        EXPORTER.panels_cli(["update", "--name", "renamed", "--new-name", "first"])
                self.assertEqual(path.read_bytes(), saved)
                self.assertEqual((path.parent / "xui-first.token").read_text().strip(), "first-token")

    def test_migration_preserves_existing_panel_and_deletion_stays_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "panels.json"
            args = ["migrate", "--name", "central", "--url",
                    "https://panel.example.com/panel/api/inbounds/list", "--logs", "true"]
            with mock.patch.object(EXPORTER, "PANELS_FILE", str(path)):
                with mock.patch("sys.stdin", io.StringIO("original-token")):
                    EXPORTER.panels_cli(args)
                original = path.read_text(encoding="utf-8")
                with mock.patch("sys.stdin", io.StringIO("replacement-token")):
                    EXPORTER.panels_cli(args[:-1] + ["false"])
                self.assertEqual(path.read_text(encoding="utf-8"), original)
                token = pathlib.Path(directory) / "xui-central.token"
                self.assertEqual(token.read_text().strip(), "original-token")
                EXPORTER.panels_cli(["delete", "--name", "central"])
                self.assertFalse(token.exists())
                with mock.patch.object(EXPORTER, "API_URL", args[4]), \
                        mock.patch.object(EXPORTER, "WORKERS", []), \
                        mock.patch("sys.argv", ["xui_exporter.py"]), \
                        mock.patch.object(EXPORTER, "ThreadingHTTPServer"), \
                        mock.patch.object(EXPORTER, "PanelWorker") as worker:
                    EXPORTER.main()
                    worker.assert_not_called()
                self.assertEqual(EXPORTER.load_config(path), [])

    def test_collects_inbound_client_and_online_metrics(self):
        inbounds = [{
            "id": 7,
            "remark": "HK-VLESS",
            "port": 443,
            "protocol": "vless",
            "up": 1024,
            "down": 2048,
            "clientStats": [{
                "email": 'user"one',
                "up": 100,
                "down": 200,
                "lastOnline": 1_725_000_000_000,
            }],
        }]

        def fake_request(url, method="GET"):
            return ["user\"one"] if method == "POST" else inbounds

        with mock.patch.object(EXPORTER, "API_URL", "https://panel.example.com/panel/api/inbounds/list"), \
                mock.patch.object(EXPORTER, "SERVER_NAME", "HY-248"), \
                mock.patch.object(EXPORTER, "request_json", side_effect=fake_request):
            output = EXPORTER.collect()

        self.assertIn('xui_exporter_up{server="HY-248"} 1', output)
        self.assertIn('direction="uplink"', output)
        self.assertIn('email="user\\"one"', output)
        self.assertIn("xui_client_online{", output)
        self.assertIn("} 1\n", output)
        self.assertIn("1725000000.000", output)
        metric_lines = [line for line in output.splitlines() if line.startswith("xui_")]
        self.assertTrue(metric_lines)
        self.assertTrue(all('server="HY-248"' in line for line in metric_lines))

    def test_global_client_is_counted_once_across_inbounds(self):
        inbounds = [{"id": 1, "up": 10, "down": 20}, {"id": 2, "up": 30, "down": 40}]
        clients = [{"email": "alice", "traffic": {"up": 8, "down": 12, "lastOnline": 1700000000000}}]
        output = EXPORTER.render_metrics("node-a", inbounds, ["alice"], clients)
        self.assertEqual(sum(line.startswith("xui_client_online{") for line in output), 1)
        self.assertEqual(sum(line.startswith("xui_client_traffic_bytes_total{") for line in output), 2)
        self.assertEqual(sum(line.startswith("xui_inbound_traffic_bytes_total{") for line in output), 4)

    def test_log_retry_checkpoint_and_duplicate_occurrences(self):
        panel = {"name": "node-a", "url": "https://panel.example.com/panel/api/inbounds/list", "log_count": 100,
                 "max_log_age": 3600}
        entry = {"DateTime": "2026-10-14T00:00:00.123456789Z", "FromAddress": "1.2.3.4:1234",
                 "ToAddress": "tcp:example.com:443", "Inbound": "in", "Outbound": "out", "Email": "alice"}
        now = 1791936000.0
        sent = []
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "push_loki", side_effect=lambda streams: sent.append(streams)):
            forwarder = EXPORTER.LogForwarder(panel, directory)
            forwarder.forward([entry, entry], now=now)
            self.assertEqual(len(sent), 1)
            self.assertEqual(len(sent[0][0]["values"]), 2)
            self.assertEqual(sent[0][0]["stream"]["email"], "alice")
            self.assertIn("accepted tcp:example.com:443", sent[0][0]["values"][0][1])
            forwarder.close()
            forwarder = EXPORTER.LogForwarder(panel, directory)
            forwarder.forward([entry, entry], now=now + 30)
            self.assertEqual(len(sent), 1)
            forwarder.close()

    def test_loki_failure_does_not_checkpoint(self):
        panel = {"name": "node-a", "url": "https://panel.example.com/panel/api/inbounds/list", "log_count": 100}
        entry = {"DateTime": "2026-10-14T00:00:00Z", "FromAddress": "1.2.3.4:1234", "ToAddress": "tcp:example.com:443"}
        now = 1791936000.0
        with tempfile.TemporaryDirectory() as directory:
            forwarder = EXPORTER.LogForwarder(panel, directory)
            with mock.patch.object(EXPORTER, "push_loki", side_effect=OSError("unavailable")):
                with self.assertRaises(OSError):
                    forwarder.forward([entry], now=now)
            with mock.patch.object(EXPORTER, "push_loki") as push:
                forwarder.forward([entry], now=now)
                push.assert_called_once()
            forwarder.close()

    def test_panels_config_validates_names_and_urls(self):
        panel = {"name": "node-a", "url": "https://panel.example.com/base/panel/api/inbounds/list",
                 "token_file": "/run/secrets/xui-node-a.token", "logs": True}
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "panels.json"
            path.write_text(json.dumps([panel]), encoding="utf-8")
            self.assertEqual(EXPORTER.load_config(path), [panel])
            panel["url"] = "http://panel.example.com/panel/api/inbounds/list"
            with self.assertRaises(ValueError):
                EXPORTER.validate_panel(panel)

    def test_log_failure_does_not_hide_traffic_metrics(self):
        panel = {"name": "node-a", "url": "https://panel.example.com/panel/api/inbounds/list",
                 "token_file": "/run/secrets/xui-node-a.token", "logs": True}
        worker = EXPORTER.PanelWorker(panel)
        inbounds = [{"id": 1, "up": 10, "down": 20}]
        def fake_request(url, method="GET", token=None, form=None):
            if "/server/xraylogs/" in url:
                raise OSError("unavailable")
            if url.endswith("/clients/onlines"):
                return []
            if url.endswith("/clients/list"):
                return []
            return inbounds
        with mock.patch.object(EXPORTER, "read_token", return_value="secret"), \
                mock.patch.object(EXPORTER, "request_json", side_effect=fake_request):
            worker.poll(mock.Mock())
        output = "\n".join(worker.snapshot())
        self.assertIn('xui_exporter_up{server="node-a"} 1', output)
        self.assertIn('xui_access_log_up{server="node-a"} 0', output)

    def test_telegram_bot_uses_metrics_for_queries_and_persists_group_subscriptions(self):
        panel = {"name": "node-a", "url": "https://panel.example.com/panel/api/inbounds/list"}
        worker = EXPORTER.PanelWorker(panel)
        worker.metrics = [
            EXPORTER.metric("xui_exporter_up", 1, server="node-a"),
            EXPORTER.metric("xui_client_online", 1, server="node-a", email="alice", group="shared"),
            EXPORTER.metric("xui_client_traffic_bytes_total", 1024, server="node-a", email="alice", group="shared", direction="uplink"),
            EXPORTER.metric("xui_client_traffic_bytes_total", 2048, server="node-a", email="alice", group="shared", direction="downlink"),
            EXPORTER.metric("xui_metrics_last_success_timestamp_seconds", 1000, server="node-a"),
        ]
        config = {"token": "bot-token", "allowed_user_ids": [42], "allowed_chat_ids": []}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot(config, [worker], directory)
            try:
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/groups"})
                self.assertIn("shared", request.call_args.args[2]["text"])
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/traffic shared"})
                self.assertIn("⬆️ 上行：1.00 KiB", request.call_args.args[2]["text"])
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/watch shared"})
                bot.process_online()  # first successful observation establishes a quiet baseline
                worker.metrics[1] = EXPORTER.metric("xui_client_online", 0, server="node-a", email="alice", group="shared")
                worker.metrics[-1] = EXPORTER.metric("xui_metrics_last_success_timestamp_seconds", 1030, server="node-a")
                bot.process_online()
                worker.metrics[1] = EXPORTER.metric("xui_client_online", 1, server="node-a", email="alice", group="shared")
                worker.metrics[-1] = EXPORTER.metric("xui_metrics_last_success_timestamp_seconds", 1060, server="node-a")
                bot.process_online()
                self.assertEqual(request.call_count, 3)
                worker.metrics[-1] = EXPORTER.metric("xui_metrics_last_success_timestamp_seconds", 1090, server="node-a")
                bot.process_online()
                self.assertEqual(request.call_count, 3)
                worker.metrics[-1] = EXPORTER.metric("xui_metrics_last_success_timestamp_seconds", 1120, server="node-a")
                bot.process_online()
                self.assertEqual(request.call_count, 3)
                worker.metrics[-1] = EXPORTER.metric("xui_metrics_last_success_timestamp_seconds", 1150, server="node-a")
                bot.process_online()
                self.assertEqual(request.call_args.args[1], "sendMessage")
                self.assertEqual(request.call_args.args[2]["chat_id"], 420)
                self.assertIn("客户端上线", request.call_args.args[2]["text"])
                self.assertIn((420, "shared"), bot.db.execute("SELECT chat_id,group_name FROM subscriptions").fetchall())
                bot.handle_message({"chat": {"id": 421}, "from": {"id": 99}, "text": "/groups"})
                self.assertEqual(request.call_count, 4)
            finally:
                bot.db.close()

    def test_telegram_config_requires_a_valid_allowlist_and_token_file(self):
        with tempfile.TemporaryDirectory() as directory:
            config_path = pathlib.Path(directory) / "telegram.json"
            token_path = pathlib.Path(directory) / "telegram-token"
            token_path.write_text("123:secret\n", encoding="utf-8")
            config_path.write_text(json.dumps({"token_file": str(token_path), "allowed_user_ids": [42]}), encoding="utf-8")
            with mock.patch.object(EXPORTER, "read_token", return_value="123:secret"):
                with self.assertRaises(ValueError):
                    EXPORTER.telegram_config(config_path)
            config_path.write_text(json.dumps({"token_file": "/run/secrets/telegram-bot.token", "allowed_user_ids": [42]}), encoding="utf-8")
            with mock.patch.object(EXPORTER, "read_token", return_value="123:secret"):
                loaded = EXPORTER.telegram_config(config_path)
                self.assertEqual(loaded["allowed_user_ids"], [42])
                self.assertEqual(loaded["notification_cooldown_seconds"], 1800)
            config_path.write_text(json.dumps({"token_file": "/run/secrets/telegram-bot.token",
                                               "allowed_user_ids": [42],
                                               "notification_cooldown_seconds": 600}), encoding="utf-8")
            with mock.patch.object(EXPORTER, "read_token", return_value="123:secret"):
                self.assertEqual(EXPORTER.telegram_config(config_path)["notification_cooldown_seconds"], 1800)
            config_path.write_text(json.dumps({"token_file": "/run/secrets/telegram-bot.token",
                                               "allowed_user_ids": [42],
                                               "notification_cooldown_seconds": 1200}), encoding="utf-8")
            with mock.patch.object(EXPORTER, "read_token", return_value="123:secret"):
                self.assertEqual(EXPORTER.telegram_config(config_path)["notification_cooldown_seconds"], 1200)
            for invalid in (-1, 86401, True, "600"):
                with self.subTest(cooldown=invalid), self.assertRaises(ValueError):
                    EXPORTER.validate_notification_cooldown(invalid)

    def test_telegram_admin_update_preserves_notification_cooldown(self):
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "telegram-config.json"
            path.write_text(json.dumps({"token_file": "/run/secrets/telegram-bot.token",
                                        "allowed_user_ids": [42],
                                        "notification_cooldown_seconds": 1200}), encoding="utf-8")
            with mock.patch.object(EXPORTER, "TELEGRAM_CONFIG_FILE", str(path)), \
                    mock.patch.object(sys, "stdin", io.StringIO("987:bot-token")):
                EXPORTER.telegram_cli(["set", "--user-ids", "77,42,77"])
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["allowed_user_ids"], [42, 77])
            self.assertEqual(saved["notification_cooldown_seconds"], 1200)
            self.assertEqual((path.parent / "telegram-bot.token").read_text().strip(), "987:bot-token")

    def test_telegram_registers_native_command_menu_and_exposes_id_safely(self):
        worker = EXPORTER.PanelWorker({"name": "node-a", "url": "https://panel.example.com/panel/api/inbounds/list"})
        worker.metrics = [EXPORTER.metric("xui_exporter_up", 1, server="node-a")]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, [worker], directory)
            try:
                bot.register_commands()
                methods = [call.args[1] for call in request.call_args_list]
                self.assertIn("setChatMenuButton", methods)
                self.assertIn("setMyCommands", methods)
                menu = request.call_args_list[methods.index("setChatMenuButton")].args[2]
                self.assertEqual(json.loads(menu["menu_button"]), {"type": "commands"})
                command_call = request.call_args_list[methods.index("setMyCommands")]
                commands = json.loads(command_call.args[2]["commands"])
                self.assertEqual(commands[0], {"command": "start", "description": "显示主菜单"})
                self.assertIn({"command": "id", "description": "显示您的 Telegram ID"}, commands)
                bot.handle_message({"chat": {"id": 999}, "from": {"id": 77}, "text": "/id"})
                self.assertIn("77", request.call_args.args[2]["text"])
                self.assertIn("999", request.call_args.args[2]["text"])
                previous_calls = request.call_count
                bot.handle_message({"chat": {"id": 999}, "from": {"id": 77}, "text": "/status"})
                self.assertEqual(request.call_count, previous_calls)
                bot.handle_message({"chat": {"id": 999}, "from": {"id": 42}, "text": "/status"})
                self.assertIn("机器人运行正常", request.call_args.args[2]["text"])
                self.assertIn("node-a：正常", request.call_args.args[2]["text"])
            finally:
                bot.db.close()

    def test_telegram_old_button_labels_execute_the_corresponding_commands(self):
        worker = EXPORTER.PanelWorker({"name": "node-a"})
        worker.metrics = EXPORTER.render_metrics("node-a", [], ["alice"], [
            {"email": "alice", "group": "Aurora 极光云瓶", "traffic": {"up": 1024, "down": 2048}}])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request, \
                mock.patch.object(EXPORTER.TelegramBot, "report_time", return_value="2026-10-14 18:00:00（UTC+8）"):
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, [worker], directory)
            try:
                for label, command in bot.BUTTON_COMMANDS.items():
                    with self.subTest(label=label):
                        message = {"chat": {"id": 420}, "from": {"id": 42}}
                        bot.handle_message(dict(message, text=command))
                        expected = request.call_args.args[2]
                        request.reset_mock()
                        bot.handle_message(dict(message, text=label))
                        self.assertEqual(request.call_args.args[2], expected)
                        self.assertEqual(json.loads(request.call_args_list[0].args[2]["reply_markup"]),
                                         {"remove_keyboard": True})
                        self.assertIn("inline_keyboard", json.loads(request.call_args_list[1].args[2]["reply_markup"]))
                for row in bot.main_keyboard()["inline_keyboard"]:
                    for button in row:
                        with self.subTest(callback=button["callback_data"]):
                            bot.handle_callback({"id": "menu-query", "from": {"id": 42},
                                                 "message": {"chat": {"id": 420}}, "data": button["callback_data"]})
                            actual = request.call_args.args[2]
                            bot.handle_message({"chat": {"id": 420}, "from": {"id": 42},
                                                "text": bot.BUTTON_COMMANDS[button["text"]]})
                            self.assertEqual(actual, request.call_args.args[2])
            finally:
                bot.db.close()

    def test_telegram_reports_flag_unavailable_panels_and_keep_long_results_complete(self):
        workers = [EXPORTER.PanelWorker({"name": "node-a"}), EXPORTER.PanelWorker({"name": "node-b"})]
        workers[0].client_inbounds = {"alice": ["HK-443", "fallback"]}
        workers[0].metrics = EXPORTER.render_metrics("node-a", [], ["alice"], [
            {"email": "alice", "group": "Aurora", "traffic": {"up": 1024, "down": 2048}}])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request, \
                mock.patch.object(EXPORTER, "time") as clock:
            clock.time.return_value = 1791936000  # 2026-10-14 08:00:00 UTC+8
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, workers, directory)
            try:
                bot.handle_message({"chat": {"id": 42}, "from": {"id": 42}, "text": "/online"})
                report = request.call_args.args[2]["text"]
                self.assertIn("👤 客户端：alice\n👥 分组：Aurora\n🔗 关联入站：HK-443、fallback\n🖥 面板：node-a", report)
                self.assertIn("📊 总计：3.00 KiB", report)
                self.assertIn("未计入本次结果：node-b", report)
                self.assertIn("2026-10-14 08:00:00（UTC+8）", report)
                request.reset_mock()
                content = "👤 客户端：alice\n⬆️ 上行：1.00 KiB\n\n" * 200
                bot.send(42, content, bot.main_keyboard())
                messages = [call.args[2] for call in request.call_args_list]
                self.assertGreater(len(messages), 1)
                self.assertEqual("".join(message["text"] for message in messages), content)
                self.assertTrue(all(len(message["text"].encode("utf-16-le")) // 2 <= 3500 for message in messages))
                self.assertTrue(all("reply_markup" not in message for message in messages[:-1]))
                self.assertIn("reply_markup", messages[-1])
            finally:
                bot.db.close()

    def test_telegram_start_shows_main_keyboard_and_watch_commands_offer_group_buttons(self):
        worker = EXPORTER.PanelWorker({"name": "node-a", "url": "https://panel.example.com/panel/api/inbounds/list"})
        worker.metrics = [
            EXPORTER.metric("xui_exporter_up", 1, server="node-a"),
            EXPORTER.metric("xui_client_online", 0, server="node-a", email="alice", group="Aurora 极光云瓶"),
        ]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, [worker], directory)
            try:
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/start"})
                removal = json.loads(request.call_args_list[-2].args[2]["reply_markup"])
                self.assertEqual(removal, {"remove_keyboard": True})
                keyboard = json.loads(request.call_args.args[2]["reply_markup"])
                self.assertNotIn("keyboard", keyboard)
                self.assertEqual(keyboard["inline_keyboard"][0][0]["text"], "📡 状态")
                self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "menu:status")
                bot.handle_callback({"id": "menu-query", "from": {"id": 42},
                                     "message": {"chat": {"id": 420}}, "data": "menu:status"})
                self.assertIn("机器人运行正常", request.call_args.args[2]["text"])
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/watch"})
                picker = json.loads(request.call_args.args[2]["reply_markup"])
                self.assertEqual(picker["inline_keyboard"][0][0]["callback_data"], "watch:0")
                bot.handle_callback({"id": "query-1", "from": {"id": 42},
                                     "message": {"chat": {"id": 420}}, "data": "watch:0"})
                self.assertIn((420, "Aurora 极光云瓶"), bot.db.execute(
                    "SELECT chat_id,group_name FROM subscriptions").fetchall())
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/unwatch"})
                picker = json.loads(request.call_args.args[2]["reply_markup"])
                self.assertEqual(picker["inline_keyboard"][0][0]["callback_data"], "unwatch:0")
            finally:
                bot.db.close()

    def test_online_notification_menu_manages_groups_and_sends_non_mutating_test(self):
        worker = EXPORTER.PanelWorker({"name": "node-a"})
        worker.metrics = EXPORTER.render_metrics("node-a", [], [], [
            {"email": "alice", "group": "shared", "traffic": {"up": 1024}}])
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(EXPORTER, "telegram_request") as request, \
                mock.patch.object(worker, "last_ip_for_client", return_value={
                    "ip": "203.0.113.5", "time": "2026-10-14 08:00:00"}):
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, [worker], directory)
            try:
                message = {"chat": {"id": 420}, "from": {"id": 42}}
                bot.handle_message(dict(message, text="/online_notify"))
                settings = request.call_args.args[2]
                keyboard = json.loads(settings["reply_markup"])
                self.assertEqual(keyboard["inline_keyboard"][1][0]["callback_data"], "onlinenotify:groups")
                self.assertEqual(keyboard["inline_keyboard"][3][0]["callback_data"], "onlinenotify:test")
                self.assertEqual(keyboard["inline_keyboard"][4][0]["callback_data"], "menu:home")
                bot.handle_callback({"id": "add", "from": {"id": 42}, "message": message,
                                    "data": "onlinenotify:add"})
                picker = json.loads(request.call_args.args[2]["reply_markup"])
                self.assertEqual(picker["inline_keyboard"][0][0]["callback_data"], "onlinenotify:add:0")
                bot.handle_callback({"id": "select", "from": {"id": 42}, "message": message,
                                    "data": "onlinenotify:add:0"})
                self.assertEqual(bot.db.execute("SELECT group_name FROM subscriptions WHERE chat_id=420").fetchone(),
                                 ("shared",))
                bot.handle_callback({"id": "test", "from": {"id": 42}, "message": message,
                                    "data": "onlinenotify:test"})
                text = request.call_args.args[2]["text"]
                self.assertIn("🧪 上线通知测试", text)
                self.assertIn("客户端：alice", text)
                self.assertIn("当前状态：离线", text)
                self.assertIn("最近记录 IP：203.0.113.5", text)
                self.assertEqual(bot.db.execute("SELECT COUNT(*) FROM notification_cooldowns").fetchone(), (0,))
                self.assertEqual(bot.db.execute("SELECT COUNT(*) FROM online_confirmations").fetchone(), (0,))
            finally:
                bot.db.close()

    def test_notification_settings_return_button_opens_main_menu(self):
        worker = EXPORTER.PanelWorker({"name": "node-a"})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, [worker], directory)
            try:
                message = {"chat": {"id": 420}, "from": {"id": 42}}
                for command, callback in (("/online_notify", "onlinenotify"), ("/traffic_notify", "trafficnotify")):
                    bot.handle_message(dict(message, text=command))
                    keyboard = json.loads(request.call_args.args[2]["reply_markup"])
                    self.assertEqual(keyboard["inline_keyboard"][-1][0],
                                     {"text": "↩️ 返回主菜单", "callback_data": "menu:home"})
                    bot.handle_callback({"id": callback, "from": {"id": 42}, "message": message,
                                         "data": "menu:home"})
                    self.assertEqual(request.call_args.args[1], "sendMessage")
                    self.assertIn("请选择", request.call_args.args[2]["text"])
                    self.assertIn("inline_keyboard", json.loads(request.call_args.args[2]["reply_markup"]))
            finally:
                bot.db.close()

    def test_online_notification_toggle_persists_and_only_pauses_selected_chat(self):
        worker = EXPORTER.PanelWorker({"name": "node-a"})
        clients = [{"email": "alice", "group": "shared", "traffic": {}}]
        config = {"token": "bot-token", "allowed_user_ids": [42], "notification_cooldown_seconds": 0}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot(config, [worker], directory)
            try:
                with bot.db:
                    bot.db.executemany("INSERT INTO subscriptions VALUES (?, 'shared')", [(420,), (421,)])
                self.assertIn("上线通知：开启", bot.online_notify_keyboard(420)[0])
                bot.handle_callback({"id": "off", "from": {"id": 42},
                                     "message": {"chat": {"id": 420}}, "data": "onlinenotify:toggle:0"})
                self.assertIn("上线通知：关闭", request.call_args.args[2]["text"])
                bot.db.close()
                bot = EXPORTER.TelegramBot(config, [worker], directory)
                self.assertIn("上线通知：关闭", bot.online_notify_keyboard(420)[0])
                self.assertEqual(bot.db.execute("SELECT group_name FROM subscriptions WHERE chat_id=420").fetchone(),
                                 ("shared",))

                def sample(timestamp, online):
                    with mock.patch.object(EXPORTER.time, "time", return_value=timestamp):
                        worker.metrics = EXPORTER.render_metrics("node-a", [], ["alice"] if online else [], clients)
                    bot.process_online()

                request.reset_mock()
                sample(1000, False)
                sample(1030, True)
                sample(1060, True)
                sample(1090, True)
                sample(1120, True)
                self.assertEqual([call.args[2]["chat_id"] for call in request.call_args_list], [421])
                self.assertIsNone(bot.db.execute(
                    "SELECT notified_at FROM notification_cooldowns WHERE chat_id=420").fetchone())
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/online_notify on"})
                request.reset_mock()
                sample(1150, True)  # Enabling does not replay an already confirmed client.
                request.assert_not_called()
                sample(1180, False)
                sample(1210, True)
                sample(1240, True)
                sample(1270, True)
                sample(1300, True)
                self.assertEqual({call.args[2]["chat_id"] for call in request.call_args_list}, {420, 421})
            finally:
                bot.db.close()

    def test_notification_group_views_and_my_subscriptions_are_separate(self):
        config = {"token": "bot-token", "allowed_user_ids": [42]}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot(config, [], directory)
            try:
                with bot.db:
                    bot.db.execute("INSERT INTO subscriptions VALUES (420,'online-group')")
                    bot.db.execute("INSERT INTO online_notify_settings VALUES (420,0)")
                    bot.db.execute("INSERT INTO traffic_notify_groups VALUES (420,'traffic-group')")
                    bot.db.execute("INSERT INTO traffic_notify_settings VALUES (420,1,'20:00')")
                for prefix, expected, excluded, keyboard in (
                        ("onlinenotify", "online-group", "traffic-group", bot.online_notify_keyboard(420)[1]),
                        ("trafficnotify", "traffic-group", "online-group", bot.traffic_notify_keyboard(420)[1])):
                    actions = [button["callback_data"] for row in keyboard["inline_keyboard"] for button in row]
                    self.assertLess(actions.index(prefix + ":groups"), actions.index(prefix + ":add"))
                    self.assertLess(actions.index(prefix + ":groups"), actions.index(prefix + ":remove"))
                    bot.handle_callback({"id": prefix, "from": {"id": 42},
                                         "message": {"chat": {"id": 420}}, "data": prefix + ":groups"})
                    text = request.call_args.args[2]["text"]
                    self.assertIn(expected, text)
                    self.assertNotIn(excluded, text)
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/watches"})
                text = request.call_args.args[2]["text"]
                self.assertIn("上线通知：关闭\n  online-group", text)
                self.assertIn("流量通知：开启\n  traffic-group", text)
                request.reset_mock()
                bot.handle_callback({"id": "unauthorized", "from": {"id": 99},
                                     "message": {"chat": {"id": 420}}, "data": "onlinenotify:toggle:1"})
                self.assertEqual(bot.db.execute("SELECT enabled FROM online_notify_settings").fetchone(), (0,))
                self.assertEqual(request.call_args.args[1], "answerCallbackQuery")
            finally:
                bot.db.close()

    def test_telegram_scheduled_traffic_notifications_summarize_groups_and_optional_panels(self):
        workers = [EXPORTER.PanelWorker({"name": name}) for name in ("node-a", "node-b")]
        clients = [
            {"email": "alice", "group": "shared", "traffic": {"up": 1024, "down": 2048}},
            {"email": "bob", "group": "shared", "traffic": {"up": 512, "down": 512}},
            {"email": "carol", "group": "other", "traffic": {"up": 4096, "down": 4096}},
        ]
        workers[0].metrics = EXPORTER.render_metrics("node-a", [], [], clients[:2])
        workers[1].metrics = EXPORTER.render_metrics("node-b", [], [], clients[1:])
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, workers, directory)
            try:
                with bot.db:
                    bot.db.execute("INSERT INTO traffic_notify_settings VALUES (420,1,'20:00')")
                    bot.db.execute("INSERT INTO traffic_notify_groups VALUES (420,'shared')")
                before_time = int(EXPORTER.datetime(2026, 10, 14, 19, 59, tzinfo=EXPORTER.timezone(
                    EXPORTER.timedelta(hours=8))).timestamp())
                bot.process_traffic_notifications(before_time)
                request.assert_not_called()

                report_time = int(EXPORTER.datetime(2026, 10, 14, 20, 0, tzinfo=EXPORTER.timezone(
                    EXPORTER.timedelta(hours=8))).timestamp())
                bot.process_traffic_notifications(report_time)
                first = request.call_args.args[2]["text"]
                self.assertNotIn("🖥 面板：", first)
                self.assertIn("👥 分组：shared\n⬆️ 上行：2.00 KiB\n⬇️ 下行：3.00 KiB\n📊 总计：5.00 KiB", first)
                self.assertIn("🆕 较上次新增：首次记录", first)
                self.assertNotIn("分组：other", first)
                request.reset_mock()
                bot.process_traffic_notifications(report_time + 300)
                request.assert_not_called()
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42},
                                    "text": "/traffic_notify panels on"})
                request.reset_mock()
                bot.process_traffic_notifications(report_time + 600)
                request.assert_not_called()  # Changing presentation does not resend today's report.

                workers[0].metrics = EXPORTER.render_metrics("node-a", [], [], [
                    {"email": "alice", "group": "shared", "traffic": {"up": 2048, "down": 3072}},
                    {"email": "bob", "group": "shared", "traffic": {"up": 512, "down": 512}},
                ])
                workers[1].metrics = EXPORTER.render_metrics("node-b", [], [], [
                    {"email": "bob", "group": "shared", "traffic": {"up": 1024, "down": 1536}},
                ])
                next_day = report_time + 86400
                bot.process_traffic_notifications(next_day)
                second = request.call_args.args[2]["text"]
                self.assertIn("👥 分组：shared\n⬆️ 上行：3.50 KiB\n⬇️ 下行：5.00 KiB\n📊 总计：8.50 KiB", second)
                self.assertIn("➕ 较上次新增：3.50 KiB（⬆️ 1.50 KiB / ⬇️ 2.00 KiB）", second)
                self.assertEqual(second.count("🖥 面板：node-a"), 1)
                self.assertEqual(second.count("🖥 面板：node-b"), 1)
                self.assertIn("➕ 较上次新增：2.00 KiB（⬆️ 1.00 KiB / ⬇️ 1.00 KiB）", second)
                self.assertIn("➕ 较上次新增：1.50 KiB（⬆️ 512 B / ⬇️ 1.00 KiB）", second)
            finally:
                bot.db.close()

    def test_changed_traffic_time_rearms_today_and_survives_restart(self):
        worker = EXPORTER.PanelWorker({"name": "node-a"})
        config = {"token": "bot-token", "allowed_user_ids": [42]}
        message = {"chat": {"id": 420}, "from": {"id": 42}}
        report_time = EXPORTER.datetime(2026, 10, 14, 20, 0, tzinfo=EXPORTER.timezone(
            EXPORTER.timedelta(hours=8))).timestamp()
        due_time = report_time + 3 * 3600 + 20 * 60
        for use_prompt in (False, True):
            with self.subTest(use_prompt=use_prompt), tempfile.TemporaryDirectory() as directory, \
                    mock.patch.object(EXPORTER, "telegram_request") as request:
                worker.metrics = EXPORTER.render_metrics("node-a", [], [], [
                    {"email": "alice", "group": "shared", "traffic": {"up": 1024, "down": 2048}}])
                bot = EXPORTER.TelegramBot(config, [worker], directory)
                try:
                    with bot.db:
                        bot.db.execute("INSERT INTO traffic_notify_settings VALUES (420,1,'20:00')")
                        bot.db.execute("INSERT INTO traffic_notify_groups VALUES (420,'shared')")
                        bot.db.execute("INSERT INTO traffic_notify_runs VALUES (421,'2026-10-14')")
                    bot.process_traffic_notifications(report_time)
                    self.assertEqual(bot.db.execute(
                        "SELECT sent_date FROM traffic_notify_runs WHERE chat_id=420").fetchone(),
                        ("2026-10-14",))
                    baseline = bot.db.execute("SELECT * FROM traffic_notify_baselines").fetchall()
                    if use_prompt:
                        bot.handle_callback({"id": "time", "from": {"id": 42},
                                             "message": {"chat": {"id": 420}},
                                             "data": "trafficnotify:time"})
                        bot.handle_message(dict(message, text="23:20"))
                    else:
                        bot.handle_message(dict(message, text="/traffic_notify time 23:20"))
                    self.assertEqual(bot.db.execute(
                        "SELECT enabled,notify_time FROM traffic_notify_settings WHERE chat_id=420").fetchone(),
                        (1, "23:20"))
                    self.assertIsNone(bot.db.execute(
                        "SELECT sent_date FROM traffic_notify_runs WHERE chat_id=420").fetchone())
                    self.assertEqual(bot.db.execute(
                        "SELECT sent_date FROM traffic_notify_runs WHERE chat_id=421").fetchone(),
                        ("2026-10-14",))
                    self.assertEqual(bot.db.execute("SELECT * FROM traffic_notify_baselines").fetchall(), baseline)
                    bot.db.close()
                    bot = EXPORTER.TelegramBot(config, [worker], directory)
                    request.reset_mock()
                    bot.process_traffic_notifications(due_time - 60)
                    request.assert_not_called()
                    worker.metrics = EXPORTER.render_metrics("node-a", [], [], [
                        {"email": "alice", "group": "shared", "traffic": {"up": 2048, "down": 3072}}])
                    request.side_effect = RuntimeError("temporary send failure")
                    with mock.patch.object(sys, "stderr", io.StringIO()):
                        bot.process_traffic_notifications(due_time)
                    self.assertIsNone(bot.db.execute(
                        "SELECT sent_date FROM traffic_notify_runs WHERE chat_id=420").fetchone())
                    self.assertEqual(bot.db.execute("SELECT * FROM traffic_notify_baselines").fetchall(), baseline)
                    request.side_effect = None
                    request.reset_mock()
                    bot.process_traffic_notifications(due_time + 20)
                    request.assert_called_once()
                    self.assertIn("2026-10-14 23:20", request.call_args.args[2]["text"])
                    self.assertIn("➕ 较上次新增：2.00 KiB", request.call_args.args[2]["text"])
                    # Re-entering the same time or an invalid time must not rearm delivery.
                    bot.handle_message(dict(message, text="/traffic_notify time 23:20"))
                    bot.handle_message(dict(message, text="/traffic_notify time 24:00"))
                    request.reset_mock()
                    bot.process_traffic_notifications(due_time + 60)
                    request.assert_not_called()
                finally:
                    bot.db.close()

    def test_telegram_traffic_notification_settings_and_time_validation(self):
        worker = EXPORTER.PanelWorker({"name": "node-a"})
        worker.metrics = EXPORTER.render_metrics("node-a", [], [], [
            {"email": "alice", "group": "shared", "traffic": {}}])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, [worker], directory)
            message = {"chat": {"id": 420}, "from": {"id": 42}}
            try:
                bot.handle_message(dict(message, text="/traffic_notify"))
                self.assertIn("每天 20:00", request.call_args.args[2]["text"])
                keyboard = json.loads(request.call_args.args[2]["reply_markup"])
                self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "trafficnotify:toggle:1")
                bot.handle_callback({"id": "toggle", "from": {"id": 42},
                                     "message": {"chat": {"id": 420}}, "data": "trafficnotify:toggle:1"})
                self.assertEqual(bot.db.execute(
                    "SELECT enabled FROM traffic_notify_settings WHERE chat_id=420").fetchone(), (1,))
                bot.handle_message(dict(message, text="/traffic_notify time 8:00"))
                self.assertEqual(bot.db.execute(
                    "SELECT notify_time FROM traffic_notify_settings WHERE chat_id=420").fetchone(), ("08:00",))
                bot.handle_message(dict(message, text="/traffic_notify time 08:15"))
                self.assertEqual(bot.db.execute(
                    "SELECT enabled,notify_time FROM traffic_notify_settings WHERE chat_id=420").fetchone(), (1, "08:15"))
                bot.handle_message(dict(message, text="/traffic_notify add shared"))
                self.assertEqual(bot.db.execute(
                    "SELECT group_name FROM traffic_notify_groups WHERE chat_id=420").fetchone(), ("shared",))
                self.assertEqual(bot.main_keyboard()["inline_keyboard"][3][0]["text"], "📊 流量通知")
            finally:
                bot.db.close()

    def test_traffic_notification_test_sends_current_data_without_changing_state(self):
        worker = EXPORTER.PanelWorker({"name": "node-a"})
        worker.metrics = EXPORTER.render_metrics("node-a", [], [], [
            {"email": "alice", "group": "shared", "traffic": {"up": 1024, "down": 2048}}])
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, [worker], directory)
            try:
                with bot.db:
                    bot.db.execute("INSERT INTO traffic_notify_settings VALUES (420,1,'20:00')")
                    bot.db.execute("INSERT INTO traffic_notify_groups VALUES (420,'shared')")
                    bot.db.execute("INSERT INTO traffic_notify_baselines VALUES (420,'shared','node-a',512,1024)")
                    bot.db.execute("INSERT INTO traffic_notify_runs VALUES (420,'2026-10-13')")
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/traffic_notify test"})
                text = request.call_args.args[2]["text"]
                self.assertIn("🧪 流量通知测试", text)
                self.assertNotIn("🖥 面板：node-a", text)
                self.assertIn("📊 总计：3.00 KiB", text)
                self.assertIn("➕ 较上次新增：1.50 KiB", text)
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42},
                                    "text": "/traffic_notify panels on"})
                bot.send_traffic_notify_test(420)
                self.assertIn("🖥 面板：node-a", request.call_args.args[2]["text"])
                self.assertEqual(bot.db.execute("SELECT * FROM traffic_notify_baselines").fetchone(),
                                 (420, "shared", "node-a", 512, 1024))
                self.assertEqual(bot.db.execute("SELECT * FROM traffic_notify_runs").fetchone(),
                                 (420, "2026-10-13"))
            finally:
                bot.db.close()

    def test_traffic_panel_option_upgrades_old_database_and_persists_per_chat(self):
        config = {"token": "bot-token", "allowed_user_ids": [42]}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            # An existing installation has no table for the new presentation option.
            with EXPORTER.sqlite3.connect(str(pathlib.Path(directory) / "telegram.sqlite")) as legacy:
                legacy.execute("CREATE TABLE traffic_notify_settings (chat_id INTEGER PRIMARY KEY, enabled INTEGER, notify_time TEXT)")
                legacy.execute("INSERT INTO traffic_notify_settings VALUES (420,1,'23:20')")
            legacy.close()
            bot = EXPORTER.TelegramBot(config, [], directory)
            try:
                self.assertFalse(bot.traffic_notify_show_panels(420))
                callback = {"id": "panels", "from": {"id": 42}, "message": {"chat": {"id": 420}}}
                bot.handle_callback(dict(callback, data="trafficnotify:panels:1"))
                self.assertTrue(bot.traffic_notify_show_panels(420))
                self.assertFalse(bot.traffic_notify_show_panels(421))
                self.assertIn("🖥 面板明细：开启", request.call_args.args[2]["text"])
                self.assertEqual(bot.db.execute("SELECT * FROM traffic_notify_settings").fetchone(),
                                 (420, 1, "23:20"))
                bot.db.close()
                bot = EXPORTER.TelegramBot(config, [], directory)
                self.assertTrue(bot.traffic_notify_show_panels(420))
                bot.handle_callback(dict(callback, data="trafficnotify:panels:2"))
                self.assertTrue(bot.traffic_notify_show_panels(420))
                bot.handle_callback(dict(callback, data="trafficnotify:panels:0", **{"from": {"id": 99}}))
                self.assertTrue(bot.traffic_notify_show_panels(420))
                bot.handle_callback(dict(callback, data="trafficnotify:panels:0"))
                self.assertFalse(bot.traffic_notify_show_panels(420))
            finally:
                bot.db.close()

    def test_traffic_panel_query_buttons_keep_group_filter_and_do_not_change_baselines(self):
        workers = [EXPORTER.PanelWorker({"name": name}) for name in ("node-a", "node-b", "node-c")]
        group = "Aurora 极光云瓶 " + "长分组名" * 20
        workers[0].metrics = EXPORTER.render_metrics("node-a", [], [], [
            {"email": "alice", "group": group, "traffic": {"up": 1024, "down": 2048}},
            {"email": "bob", "group": "other", "traffic": {"up": 4096, "down": 4096}}])
        workers[1].metrics = EXPORTER.render_metrics("node-b", [], [], [
            {"email": "alice", "group": group, "traffic": {"up": 512, "down": 512}}])
        workers[2].metrics = [EXPORTER.metric("xui_exporter_up", 0, server="node-c")]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, workers, directory)
            message = {"chat": {"id": 420}, "from": {"id": 42}}
            callback = {"id": "view", "from": {"id": 42}, "message": {"chat": {"id": 420}}}
            try:
                bot.handle_message(dict(message, text="/traffic " + group))
                report = request.call_args.args[2]
                self.assertIn("📊 总计：4.00 KiB", report["text"])
                panel_button = json.loads(report["reply_markup"])["inline_keyboard"][0][0]
                self.assertLessEqual(len(panel_button["callback_data"].encode()), 64)
                bot.handle_callback(dict(callback, data=panel_button["callback_data"]))
                report = request.call_args.args[2]
                self.assertIn("🖥 面板：node-a\n⬆️ 上行：1.00 KiB\n⬇️ 下行：2.00 KiB\n📊 总计：3.00 KiB", report["text"])
                self.assertIn("🖥 面板：node-b", report["text"])
                self.assertNotIn("8.00 KiB", report["text"])
                self.assertIn("以下面板暂不可用，未计入本次结果：node-c", report["text"])
                group_button = json.loads(report["reply_markup"])["inline_keyboard"][0][0]
                bot.handle_callback(dict(callback, data=group_button["callback_data"]))
                self.assertIn("📊 总计：4.00 KiB", request.call_args.args[2]["text"])
                bot.handle_message(dict(message, text="/traffic_panels"))
                self.assertIn("📊 总计：11.00 KiB", request.call_args.args[2]["text"])
                self.assertFalse(bot.traffic_notify_show_panels(420))
                self.assertEqual(bot.db.execute("SELECT * FROM traffic_notify_baselines").fetchall(), [])
                request.reset_mock()
                bot.handle_callback(dict(callback, data=panel_button["callback_data"], **{"from": {"id": 99}}))
                request.assert_called_once()
                self.assertEqual(request.call_args.args[1], "answerCallbackQuery")
                bot.pending_traffic_views.clear()
                bot.handle_callback(dict(callback, data=panel_button["callback_data"]))
                self.assertIn("选项已过期", request.call_args.args[2]["text"])
            finally:
                bot.db.close()

    def test_group_and_panel_deltas_handle_counter_resets_new_data_and_outages(self):
        workers = [EXPORTER.PanelWorker({"name": name}) for name in ("node-a", "node-b", "node-c")]
        workers[0].metrics = EXPORTER.render_metrics("node-a", [], [], [
            {"email": "alice", "group": "shared", "traffic": {"up": 100, "down": 200}},
            {"email": "other", "group": "second", "traffic": {"up": 10, "down": 20}},
            {"email": "ignored", "group": "unsubscribed", "traffic": {"up": 9999, "down": 9999}}])
        workers[1].metrics = EXPORTER.render_metrics("node-b", [], [], [
            {"email": "bob", "group": "shared", "traffic": {"up": 1500, "down": 2500}},
            {"email": "new", "group": "second", "traffic": {"up": 50, "down": 50}}])
        workers[2].metrics = [EXPORTER.metric("xui_exporter_up", 0, server="node-c")]
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, workers, directory)
            try:
                with bot.db:
                    bot.db.execute("INSERT INTO traffic_notify_settings VALUES (420,1,'20:00')")
                    bot.db.executemany("INSERT INTO traffic_notify_groups VALUES (420,?)", [("shared",), ("second",), ("missing",)])
                    bot.db.executemany("INSERT INTO traffic_notify_baselines VALUES (420,?,?,?,?)", [
                        ("shared", "node-a", 1000, 2000), ("shared", "node-b", 1000, 2000),
                        ("shared", "node-c", 9000, 9000), ("second", "node-a", 5, 10)])
                bot.set_traffic_notify_panels(420, True)
                now = EXPORTER.datetime(2026, 10, 14, 20, 0, tzinfo=EXPORTER.timezone(
                    EXPORTER.timedelta(hours=8))).timestamp()
                bot.process_traffic_notifications(now)
                text = request.call_args.args[2]["text"]
                self.assertIn("👥 分组：shared\n⬆️ 上行：1.56 KiB\n⬇️ 下行：2.64 KiB\n📊 总计：4.20 KiB\n"
                              "➕ 较上次新增：1000 B（⬆️ 500 B / ⬇️ 500 B）", text)
                self.assertIn("👥 分组：missing\n⚠️ 当前没有可用面板的流量数据", text)
                self.assertIn("部分流量首次记录", text)
                self.assertIn("🖥 面板：node-a\n⬆️ 上行：110 B\n⬇️ 下行：220 B\n📊 总计：330 B\n"
                              "➕ 较上次新增：15 B", text)
                self.assertEqual(text.count("🖥 面板：node-a"), 1)
                self.assertNotIn("unsubscribed", text)
                self.assertEqual(bot.db.execute(
                    "SELECT up,down FROM traffic_notify_baselines WHERE server='node-c'").fetchone(), (9000, 9000))
            finally:
                bot.db.close()

    def test_telegram_time_prompt_keeps_waiting_after_invalid_input(self):
        worker = EXPORTER.PanelWorker({"name": "node-a"})
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, [worker], directory)
            try:
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/traffic_notify"})
                bot.handle_callback({"id": "time", "from": {"id": 42}, "message": {"chat": {"id": 420}},
                                     "data": "trafficnotify:time"})
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "8:0"})
                self.assertIn(420, bot.pending_traffic_times)
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "08:00"})
                self.assertNotIn(420, bot.pending_traffic_times)
                self.assertEqual(bot.db.execute(
                    "SELECT notify_time FROM traffic_notify_settings WHERE chat_id=420").fetchone(), ("08:00",))
            finally:
                bot.db.close()

    def test_telegram_online_confirmation_filters_probes_and_repeated_samples(self):
        panel = EXPORTER.PanelWorker({"name": "node-a"})
        emails = [f"client-{index}" for index in range(6)]
        clients = [{"email": email, "group": "shared", "traffic": {}} for email in emails]
        config = {"token": "bot-token", "allowed_user_ids": [42]}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot(config, [panel], directory)
            try:
                with bot.db:
                    bot.db.execute("INSERT INTO subscriptions VALUES (?,?)", (420, "shared"))

                def sample(timestamp, online):
                    with mock.patch.object(EXPORTER.time, "time", return_value=timestamp):
                        panel.metrics = EXPORTER.render_metrics("node-a", [], online, clients)
                    bot.process_online()

                sample(1000, [])  # quiet baseline
                sample(1030, emails)  # six simultaneous delay probes
                for _ in range(3):
                    bot.process_online()  # same API result cannot confirm online
                request.assert_not_called()
                sample(1060, [])  # a successful offline sample resets all streaks
                sample(1090, emails)
                request.assert_not_called()

                # An unsuccessful API poll neither confirms nor clears pending online.
                panel.metrics = [EXPORTER.metric("xui_exporter_up", 0, server="node-a")]
                bot.process_online()
                request.assert_not_called()
                self.assertEqual({row[0] for row in bot.db.execute(
                    "SELECT consecutive_online FROM online_confirmations")}, {1})

                # Pending confirmation survives a restart; the cached result is still one sample.
                bot.db.close()
                bot = EXPORTER.TelegramBot(config, [panel], directory)
                sample(1090, emails)
                request.assert_not_called()
                sample(1120, [emails[0]])  # second consecutive online sample
                request.assert_not_called()
                sample(1150, [emails[0]])  # third consecutive online sample
                request.assert_not_called()
                sample(1180, [emails[0]])  # fourth consecutive online sample
                request.assert_called_once()
                self.assertIn(f"客户端：{emails[0]}", request.call_args.args[2]["text"])
                sample(1210, [emails[0]])  # prolonged online does not repeat notifications
                request.assert_called_once()
                self.assertEqual(bot.db.execute(
                    "SELECT consecutive_online FROM online_confirmations WHERE email=?",
                    (emails[0],)).fetchone(), (4,))
            finally:
                bot.db.close()

    def test_telegram_online_confirmation_migrates_old_online_state_quietly(self):
        panels = [EXPORTER.PanelWorker({"name": name}) for name in ("node-a", "node-b")]
        clients = [{"email": "alice", "group": "shared", "traffic": {}}]
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot({"token": "bot-token", "allowed_user_ids": [42]}, panels, directory)
            try:
                # Simulate upgrading a database from before online confirmation support.
                with bot.db:
                    bot.db.execute("INSERT INTO metadata VALUES ('baseline','1')")
                    bot.db.execute("INSERT INTO subscriptions VALUES (420,'shared')")
                    for panel in panels:
                        bot.db.execute("INSERT INTO state VALUES (?, 'alice', 'shared', 1)",
                                       (panel.panel["name"],))

                def sample(timestamp, online_a, online_b):
                    with mock.patch.object(EXPORTER.time, "time", return_value=timestamp):
                        for panel, online in zip(panels, (online_a, online_b)):
                            panel.metrics = EXPORTER.render_metrics(
                                panel.panel["name"], [], ["alice"] if online else [], clients)
                    bot.process_online()

                sample(1000, False, True)  # offline migration must reset node-a's old state
                sample(1030, True, True)
                request.assert_not_called()
                sample(1060, True, True)
                request.assert_not_called()
                sample(1090, True, True)
                request.assert_not_called()
                sample(1120, True, True)
                request.assert_called_once()
                self.assertIn("面板：node-a", request.call_args.args[2]["text"])
            finally:
                bot.db.close()

    def test_telegram_online_notifications_have_persistent_per_client_cooldown(self):
        panel = EXPORTER.PanelWorker({"name": "node-a"})
        panel.metrics = EXPORTER.render_metrics("node-a", [], [], [
            {"email": "alice", "group": "shared", "traffic": {}},
            {"email": "bob", "group": "shared", "traffic": {}},
        ])
        config = {"token": "bot-token", "allowed_user_ids": [42], "notification_cooldown_seconds": 1800}
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(EXPORTER, "telegram_request") as request, \
                mock.patch.object(EXPORTER, "time") as clock, \
                mock.patch.object(EXPORTER.TelegramBot, "report_time", return_value="test-time"):
            now = [1000]
            clock.time.side_effect = lambda: now[0]
            bot = EXPORTER.TelegramBot(config, [panel], directory)
            try:
                for chat_id in (420, 421):
                    with bot.db:
                        bot.db.execute("INSERT INTO subscriptions VALUES (?,?)", (chat_id, "shared"))
                bot.process_online()  # baseline
                self.assertEqual(request.call_count, 0)

                sample_at = [2000]
                def set_online(alice, bob):
                    sample_at[0] += 30
                    with mock.patch.object(clock, "time", return_value=sample_at[0]):
                        panel.metrics = EXPORTER.render_metrics(
                            "node-a", [], [email for email, online in (("alice", alice), ("bob", bob)) if online],
                            [{"email": "alice", "group": "shared", "traffic": {}},
                             {"email": "bob", "group": "shared", "traffic": {}}])
                    bot.process_online()

                set_online(True, False)
                self.assertEqual(request.call_count, 0)
                set_online(True, False)
                self.assertEqual(request.call_count, 0)
                set_online(True, False)
                self.assertEqual(request.call_count, 0)
                set_online(True, False)
                self.assertEqual(request.call_count, 2)
                now[0] = 1200
                set_online(False, False)
                set_online(True, False)
                set_online(True, False)
                set_online(True, False)
                set_online(True, False)  # Alice still cooling down
                self.assertEqual(request.call_count, 2)
                set_online(True, True)
                set_online(True, True)
                set_online(True, True)
                set_online(True, True)  # Bob has an independent cooldown
                self.assertEqual(request.call_count, 4)

                # Recreate the bot against the same state database.
                bot.db.close()
                bot = EXPORTER.TelegramBot(config, [panel], directory)
                now[0] = 1500
                set_online(False, False)
                set_online(True, True)
                set_online(True, True)
                set_online(True, True)
                set_online(True, True)  # persisted cooldown suppresses both
                self.assertEqual(request.call_count, 4)

                set_online(False, False)
                now[0] = 3001
                set_online(True, True)
                set_online(True, True)
                set_online(True, True)
                set_online(True, True)  # cooldown expired; notify both chats
                self.assertEqual(request.call_count, 8)
                sent_chats = [call.args[2]["chat_id"] for call in request.call_args_list]
                self.assertEqual(sent_chats, [420, 421, 420, 421, 420, 421, 420, 421])
                rows = bot.db.execute("SELECT chat_id,server,email,notified_at FROM notification_cooldowns ORDER BY chat_id,email").fetchall()
                self.assertEqual(len(rows), 4)
                self.assertEqual({row[3] for row in rows}, {3001})
            finally:
                bot.db.close()


if __name__ == "__main__":
    unittest.main()
