import json
import pathlib
import unittest


DASHBOARD_PATH = pathlib.Path(__file__).parents[1] / "dashboards" / "xray-gateway.json"


class XrayGatewayDashboardTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dashboard = json.loads(DASHBOARD_PATH.read_text(encoding="utf-8"))

    def test_server_variable_discovers_panels_without_access_logs(self):
        server_variable = self.dashboard["templating"]["list"][0]
        self.assertEqual(server_variable["name"], "server")
        self.assertEqual(server_variable["allValue"], ".+")
        self.assertEqual(server_variable["datasource"]["type"], "prometheus")
        self.assertIsInstance(server_variable["query"], str)
        self.assertIn("label_values", server_variable["query"])
        self.assertIn("server", server_variable["query"])
        self.assertIn("server", server_variable["definition"])
        self.assertIn("xui_exporter_up", server_variable["query"])

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

    def test_groups_filter_client_metrics_and_logs_but_not_panel_totals(self):
        script = (DASHBOARD_PATH.parents[1] / "deploy-xray-grafana-loki-alloy.sh").read_text(encoding="utf-8")
        access = json.loads(script.split('cat >"$STACK_DIR/grafana/dashboards/xray-access.json" <<\'EOF\'\n')[1].split('\nEOF\n')[0])
        for dashboard in (self.dashboard, access):
            variables = {v["name"]: v for v in dashboard["templating"]["list"]}
            group = variables["group"]
            self.assertIn('server=~"$server"', group["query"])
            self.assertTrue(group["multi"])
            self.assertEqual(group["allValue"], ".*")  # retain pre-upgrade data without group labels
            for panel in dashboard["panels"]:
                for target in panel["targets"]:
                    expr = target["expr"]
                    if "xui_client_" in expr or 'job="xray-access"' in expr:
                        self.assertIn('group=~"$group"', expr)
                    else:
                        self.assertNotIn("$group", expr)
        client = next(v for v in access["templating"]["list"] if v["name"] == "client")
        self.assertIn('group=~"$group"', client["query"])
        inbound = next(p for p in self.dashboard["panels"] if p["id"] == 7)
        self.assertIn("全部分组", inbound["title"])


if __name__ == "__main__":
    unittest.main()
