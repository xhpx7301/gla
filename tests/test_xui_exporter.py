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


if __name__ == "__main__":
    unittest.main()
