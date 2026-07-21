import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]


def load_dashboard(name):
    return json.loads((ROOT / "dashboards" / name).read_text(encoding="utf-8"))


def panel_by_id(dashboard, panel_id):
    return next(panel for panel in dashboard["panels"] if panel["id"] == panel_id)


class DashboardTableTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.security = load_dashboard("server-security.json")
        cls.gateway = load_dashboard("xray-gateway.json")

    def assert_sorted_and_renamed(self, panel, value_field, renamed_value, renamed_labels):
        transformations = panel["transformations"]
        self.assertEqual(transformations[0]["id"], "sortBy")
        self.assertEqual(
            transformations[0]["options"]["sort"],
            [{"field": value_field, "desc": True}],
        )
        rename_map = transformations[1]["options"]["renameByName"]
        self.assertEqual(rename_map[value_field], renamed_value)
        for source, display in renamed_labels.items():
            self.assertEqual(rename_map[source], display)

    def test_security_ip_tables_are_chinese_and_sorted(self):
        ssh = panel_by_id(self.security, 8)
        fail2ban = panel_by_id(self.security, 9)
        self.assert_sorted_and_renamed(
            ssh,
            "Value #A",
            "失败次数",
            {
                "source_ip": "来源 IP",
                "geo_country": "国家/地区",
                "geo_region": "省份",
                "geo_city": "城市",
            },
        )
        self.assert_sorted_and_renamed(
            fail2ban,
            "Value #A",
            "封禁次数",
            {
                "source_ip": "封禁 IP",
                "geo_country": "国家/地区",
                "geo_region": "省份",
                "geo_city": "城市",
            },
        )
        self.assertIn("source_ip", ssh["targets"][0]["expr"])
        self.assertIn("source_ip", fail2ban["targets"][0]["expr"])
        self.assertIn("geo_country", fail2ban["targets"][0]["expr"])
        self.assertEqual(
            panel_by_id(self.security, 8)["gridPos"],
            {"x": 0, "y": 38, "w": 24, "h": 9},
        )
        self.assertEqual(
            panel_by_id(self.security, 9)["gridPos"],
            {"x": 0, "y": 47, "w": 24, "h": 9},
        )

    def test_gateway_tables_are_chinese_and_sorted(self):
        client = panel_by_id(self.gateway, 6)
        inbound = panel_by_id(self.gateway, 7)
        source = panel_by_id(self.gateway, 8)
        self.assert_sorted_and_renamed(
            client, "Value", "流量", {"email": "客户端"}
        )
        self.assert_sorted_and_renamed(
            inbound,
            "Value",
            "流量",
            {"inbound": "入站", "port": "端口", "protocol": "协议"},
        )
        self.assert_sorted_and_renamed(
            source,
            "Value #A",
            "连接次数",
            {
                "source_ip": "来源 IP",
                "geo_country": "国家/地区",
                "geo_region": "省份",
                "geo_city": "城市",
            },
        )
        self.assertIn("geo_country", source["targets"][0]["expr"])
        self.assertEqual(
            inbound["gridPos"], {"x": 0, "y": 13, "w": 24, "h": 8}
        )
        self.assertEqual(
            source["gridPos"], {"x": 0, "y": 21, "w": 24, "h": 9}
        )

    def test_dashboard_titles_and_instant_tables_are_compact(self):
        self.assertEqual(self.security["title"], "服务器安全事件与系统资源")
        self.assertEqual(self.gateway["title"], "Xray 网关流量与连接")
        self.assertEqual([item["name"] for item in self.gateway["templating"]["list"]], ["server"])

        for dashboard, panel_ids in ((self.security, (8, 9)), (self.gateway, (6, 7, 8))):
            for panel_id in panel_ids:
                organize = panel_by_id(dashboard, panel_id)["transformations"][1]
                self.assertTrue(organize["options"]["excludeByName"]["Time"])
                self.assertNotIn("Time", organize["options"]["renameByName"])

    def test_metric_freshness_cards_show_latest_sample_time(self):
        gateway_card = panel_by_id(self.gateway, 10)
        security_card = panel_by_id(self.security, 11)
        self.assertEqual(gateway_card["title"], "指标最新时间")
        self.assertEqual(security_card["title"], "指标最新时间")
        self.assertEqual(gateway_card["targets"][0]["expr"], '1000 * max(timestamp(xui_exporter_up{server=~"$server"}))')
        self.assertEqual(security_card["targets"][0]["expr"], '1000 * max(timestamp(node_time_seconds{server=~"$server"}))')
        self.assertEqual(gateway_card["fieldConfig"]["defaults"]["unit"], "dateTimeAsLocal")
        self.assertEqual(security_card["fieldConfig"]["defaults"]["unit"], "dateTimeAsLocal")

    def test_security_dashboard_shows_the_latest_parseable_ssh_failure(self):
        latest_ssh = panel_by_id(self.security, 12)
        self.assertEqual(latest_ssh["title"], "最近 20 次 SSH 失败记录")
        self.assertEqual(latest_ssh["type"], "table")
        self.assertEqual(latest_ssh["gridPos"], {"x": 0, "y": 5, "w": 24, "h": 10})
        self.assertEqual(latest_ssh["targets"][0]["maxLines"], 20)
        self.assertEqual(latest_ssh["targets"][0]["queryType"], "range")
        self.assertEqual(latest_ssh["targets"][0]["format"], "table")
        expression = latest_ssh["targets"][0]["expr"]
        for field in ("source_ip", "source_port", "attempted_user", "geo_country", "geo_region", "geo_city"):
            self.assertIn(field, expression)
        self.assertIn("尝试用户名", expression)
        self.assertNotIn("流量使用量", expression)
        self.assertIn("label_format", expression)
        self.assertEqual(latest_ssh["transformations"][0]["id"], "labelsToFields")
        self.assertEqual(latest_ssh["transformations"][1]["id"], "sortBy")
        self.assertEqual(latest_ssh["transformations"][2]["id"], "organize")
        excluded = latest_ssh["transformations"][2]["options"]["excludeByName"]
        for field in ("NewField", "labelTypes", "id"):
            self.assertTrue(excluded[field])

    def test_security_dashboard_has_aggregate_ssh_and_ufw_traffic(self):
        ssh_traffic = panel_by_id(self.security, 13)
        ufw_traffic = panel_by_id(self.security, 14)
        self.assertEqual(ssh_traffic["gridPos"], {"x": 0, "y": 31, "w": 12, "h": 7})
        self.assertEqual(ufw_traffic["gridPos"], {"x": 12, "y": 31, "w": 12, "h": 7})
        self.assertIn("gla_ssh_inbound_bytes_total", ssh_traffic["targets"][0]["expr"])
        self.assertIn("gla_ufw_default_denied_bytes_total", ufw_traffic["targets"][0]["expr"])

    def test_embedded_access_dashboard_uses_full_width_connection_tables(self):
        script = (ROOT / "deploy-xray-grafana-loki-alloy.sh").read_text(encoding="utf-8")
        self.assertIn('"title": "匹配连接数"', script)
        self.assertIn('"gridPos": { "x": 0, "y": 18, "w": 24, "h": 7 }', script)
        self.assertIn('"gridPos": { "x": 0, "y": 25, "w": 24, "h": 7 }', script)
        self.assertIn('email=~\\".+\\"', script)
        self.assertIn('inbound=~\\".+\\"', script)
        self.assertIn('"title": "最新访问日志"', script)
        self.assertIn('"maxLines": 1', script)
        self.assertIn('"title": "所选客户端近 $period 访问目标 Top 20"', script)
        self.assertIn('"expr": "topk(20, sum by (destination)', script)
        self.assertEqual(script.count('"id": "sortBy", "options": { "sort": [{ "field": "Value #A", "desc": true }] }'), 2)


if __name__ == "__main__":
    unittest.main()
