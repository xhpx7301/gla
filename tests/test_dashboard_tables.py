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


if __name__ == "__main__":
    unittest.main()
