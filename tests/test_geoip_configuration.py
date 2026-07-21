import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]


class GeoIpConfigurationTest(unittest.TestCase):
    def test_collectors_have_optional_offline_geoip_stage(self):
        for name in (
            "deploy-xray-alloy-collector.sh",
            "deploy-xray-grafana-loki-alloy.sh",
        ):
            script = (ROOT / name).read_text(encoding="utf-8")
            self.assertIn('ENABLE_GEOIP="${ENABLE_GEOIP:-auto}"', script)
            self.assertIn("GEOIP_DB_PATH", script)
            self.assertIn("stage.geoip", script)
            self.assertIn("geoip_country_name", script)
            self.assertIn("geoip_subdivision_name", script)
            self.assertIn("geoip_city_name", script)
            self.assertIn("source_ip", script)

    def test_ip_is_not_promoted_to_a_loki_label(self):
        collector = (ROOT / "deploy-xray-alloy-collector.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('source_ip = ""', collector)
        self.assertNotIn('source_ip = "source_ip"', collector)


if __name__ == "__main__":
    unittest.main()
