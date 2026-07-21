import json
import pathlib
import unittest


DASHBOARD_PATH = pathlib.Path(__file__).parents[1] / "dashboards" / "xray-gateway.json"


class XrayGatewayDashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))

    def test_server_variable_uses_loki_server_labels(self):
        server_variable = self.dashboard["templating"]["list"][0]
        self.assertEqual(server_variable["name"], "server")
        self.assertEqual(server_variable["allValue"], ".+")
        self.assertEqual(server_variable["datasource"]["type"], "loki")
        self.assertIsInstance(server_variable["query"], str)
        self.assertIn("label_values", server_variable["query"])
        self.assertIn("server", server_variable["query"])
        self.assertIn("server", server_variable["definition"])

    def test_port_field_is_not_formatted_as_bytes(self):
        panel = next(panel for panel in self.dashboard["panels"] if panel["id"] == 7)
        overrides = panel["fieldConfig"]["overrides"]
        port_override = next(
            override for override in overrides
            if override["matcher"] == {"id": "byName", "options": "port"}
        )
        properties = {item["id"]: item["value"] for item in port_override["properties"]}
        self.assertEqual(properties["unit"], "none")
        self.assertEqual(properties["decimals"], 0)

    def test_inbound_table_places_traffic_before_protocol(self):
        panel = next(panel for panel in self.dashboard["panels"] if panel["id"] == 7)
        order = panel["transformations"][1]["options"]["indexByName"]
        self.assertLess(order["Value"], order["protocol"])


if __name__ == "__main__":
    unittest.main()
