import pathlib
import unittest


ROOT = pathlib.Path(__file__).parents[1]


class CollectorModuleConfigurationTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.script = (ROOT / "deploy-xray-alloy-collector.sh").read_text(
            encoding="utf-8"
        )

    def test_host_metrics_has_an_explicit_backward_compatible_switch(self):
        self.assertIn(
            'ENABLE_HOST_METRICS="${ENABLE_HOST_METRICS:-auto}"', self.script
        )
        self.assertIn("printf 'ENABLE_HOST_METRICS=%q", self.script)
        self.assertIn(
            'auto) [ -n "$METRICS_URL" ] && ENABLE_HOST_METRICS=true',
            self.script,
        )
        self.assertIn("2.1.x and earlier inferred host metrics", self.script)

    def test_host_and_xui_metrics_are_independently_generated(self):
        self.assertIn(
            'if [ "$ENABLE_HOST_METRICS" = true ]; then\n'
            '  cat >>"$STACK_DIR/alloy/config.alloy"',
            self.script,
        )
        self.assertIn(
            'if [ -n "$XUI_API_URL" ]; then\n'
            '  cat >>"$STACK_DIR/alloy/config.alloy"',
            self.script,
        )
        self.assertIn('if [ "$METRICS_REQUIRED" = true ]; then', self.script)
        self.assertIn(
            'if [ "$ENABLE_HOST_METRICS" = true ] || [ -n "$XUI_API_URL" ]',
            self.script,
        )

    def test_manager_exposes_all_collection_modules(self):
        self.assertIn("3. 配置采集模块", self.script)
        self.assertIn("configure_modules()", self.script)
        for module in (
            "主机指标",
            "Xray 日志",
            "安全日志（SSH/Fail2ban/UFW）",
            "GeoIP 归属解析",
            "3x-ui API 流量",
            "SSH/UFW 聚合流量",
        ):
            self.assertIn(module, self.script)

        main_start = self.script.index("2. 启动采集器")
        main_modules = self.script.index("3. 配置采集模块")
        main_stop = self.script.index("4. 停止采集器")
        self.assertLess(main_start, main_modules)
        self.assertLess(main_modules, main_stop)

        module_labels = (
            "1. Xray 日志",
            "2. 安全日志（SSH/Fail2ban/UFW）",
            "3. 主机指标",
            "4. GeoIP 归属解析",
            "5. SSH/UFW 聚合流量",
            "6. 3x-ui API 流量",
        )
        positions = [self.script.index(label) for label in module_labels]
        self.assertEqual(positions, sorted(positions))

    def test_dependency_failures_are_explained_before_redeploy(self):
        for reminder in (
            "该模块需要 VictoriaMetrics Remote Write 服务",
            "地址无效：必须使用 HTTPS 并以 /api/v1/write 结尾",
            "地址无效：必须使用 HTTPS 并以 /panel/api/inbounds/list 结尾",
            "无法启用：请先启用主机指标和 VictoriaMetrics 写入",
            "无法启用：请先启用安全日志",
            "无法启用：系统需要 iptables",
            "无法启用：请先启用 UFW",
            "未找到 systemd journal，无法启用安全日志",
            "请确认 3x-ui/Xray 已启用访问日志及路径权限",
        ):
            self.assertIn(reminder, self.script)

    def test_settings_and_module_details_are_human_readable_and_safe(self):
        for label in (
            "采集器当前设置",
            "日志写入",
            "指标写入",
            "当前使用配置",
            "访问日志路径",
            "SSH Journal",
            "数据库状态",
            "主机指标依赖",
            "Panel API 地址",
            "内容已隐藏",
        ):
            self.assertIn(label, self.script)
        self.assertIn("show_xray_current_config", self.script)
        self.assertIn("show_security_current_config", self.script)
        self.assertIn("show_host_metrics_current_config", self.script)
        self.assertIn("show_geoip_current_config", self.script)
        self.assertIn("show_security_traffic_current_config", self.script)
        self.assertIn("show_xui_current_config", self.script)
        self.assertNotIn("grep -E 'server   =|url =|__path__ ='", self.script)

    def test_manager_can_diagnose_central_write_health(self):
        for text in (
            "7. 检测中央写入状态",
            "中央写入状态检测",
            "最近 10 分钟未发现写入错误",
            "认证失败（HTTP %s），请检查用户名和密码",
            "Loki 最近数据",
            "VictoriaMetrics 最近数据",
            "最新样本约 %.0f 秒前",
            "密码仅用于本次检测，不保存、不回显",
        ):
            self.assertIn(text, self.script)
        self.assertIn("loki.write.central", self.script)
        self.assertIn("prometheus.remote_write.central", self.script)
        self.assertIn("node_time_seconds", self.script)
        self.assertIn("xui_exporter_up", self.script)
        self.assertIn("curl --config -", self.script)
        self.assertIn("请输入操作编号 [0-12]", self.script)
        self.assertIn('[[ "$age" =~ ^[0-9]+([.][0-9]+)?$ ]]', self.script)


if __name__ == "__main__":
    unittest.main()
