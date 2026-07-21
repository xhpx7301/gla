import importlib.util
import pathlib
import unittest
from unittest import mock


MODULE_PATH = pathlib.Path(__file__).parents[1] / "assets" / "xui_exporter.py"
SPEC = importlib.util.spec_from_file_location("xui_exporter", MODULE_PATH)
EXPORTER = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(EXPORTER)


class ExporterTest(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
