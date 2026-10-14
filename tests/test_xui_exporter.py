import importlib.util
import io
import json
import pathlib
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
        ]
        config = {"token": "bot-token", "allowed_user_ids": [42], "allowed_chat_ids": []}
        with tempfile.TemporaryDirectory() as directory, mock.patch.object(EXPORTER, "telegram_request") as request:
            bot = EXPORTER.TelegramBot(config, [worker], directory)
            try:
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/groups"})
                self.assertIn("shared", request.call_args.args[2]["text"])
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/traffic shared"})
                self.assertIn("上行 1.00 KiB", request.call_args.args[2]["text"])
                bot.handle_message({"chat": {"id": 420}, "from": {"id": 42}, "text": "/watch shared"})
                bot.process_online()  # first successful observation establishes a quiet baseline
                worker.metrics[1] = EXPORTER.metric("xui_client_online", 0, server="node-a", email="alice", group="shared")
                bot.process_online()
                worker.metrics[1] = EXPORTER.metric("xui_client_online", 1, server="node-a", email="alice", group="shared")
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
                self.assertEqual(EXPORTER.telegram_config(config_path)["allowed_user_ids"], [42])


if __name__ == "__main__":
    unittest.main()
