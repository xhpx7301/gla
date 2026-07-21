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
            self.assertIn("geo_country_localized", script)
            self.assertIn("geo_region_localized", script)
            self.assertIn("geo_city_localized", script)
            self.assertIn('country.names.\\"zh-CN\\" || country.names.en', script)
            self.assertIn('subdivisions[0].names.\\"zh-CN\\" || subdivisions[0].names.en', script)
            self.assertIn('city.names.\\"zh-CN\\" || city.names.en', script)
            self.assertIn("source_ip", script)
            self.assertIn("prepare_geoip_database", script)
            self.assertIn("download_geoip_database", script)
            self.assertIn("raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-City.mmdb", script)
            self.assertIn("GEOIP_MIRROR_URL", script)
            self.assertIn("下载文件过小", script)
            self.assertIn("GitHub GeoLite.mmdb 镜像下载", script)
            self.assertIn("已有 GeoLite2-City.mmdb 文件路径", script)
            self.assertIn("install -m 0640", script)

    def test_ip_is_not_promoted_to_a_loki_label(self):
        collector = (ROOT / "deploy-xray-alloy-collector.sh").read_text(
            encoding="utf-8"
        )
        self.assertNotIn('source_ip = ""', collector)
        self.assertNotIn('source_ip = "source_ip"', collector)


if __name__ == "__main__":
    unittest.main()
