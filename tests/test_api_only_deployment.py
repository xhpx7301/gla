"""Render installer configs without deploying or changing host services."""
import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = pathlib.Path(__file__).parents[1]
BASH = shutil.which("bash")
if os.name == "nt":
    git_bash = pathlib.Path("D:/Git/bin/bash.exe")
    BASH = str(git_bash) if git_bash.exists() else BASH


@unittest.skipUnless(BASH, "Bash is required for installer rendering")
class ApiOnlyDeploymentTest(unittest.TestCase):
    def test_numbered_menu_update_with_enter_preserves_each_setting(self):
        script = (ROOT / "deploy-xray-grafana-loki-alloy.sh").read_text(encoding="utf-8")
        functions = script[script.index("select_api_panel() {"):script.index("update_stack() {")]
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "panels.json"
            panels = [{"name": name, "url": "https://panel.example.com/panel/api/inbounds/list",
                       "token_file": f"/run/secrets/xui-{name}.token", "logs": False,
                       "interval": 77, "log_count": 4321, "max_log_age": 7200}
                      for name in ("first", "second")]
            path.write_text(json.dumps(panels), encoding="utf-8")
            for name in ("first", "second"):
                (path.parent / f"xui-{name}.token").write_text(name + "-token\n")
            env = dict(os.environ, PYTHONUTF8="1", XUI_PANELS_FILE=path.as_posix(),
                       TEST_PYTHON=pathlib.Path(sys.executable).as_posix(),
                       TEST_EXPORTER=(ROOT / "assets/xui_exporter.py").as_posix())
            harness = '''set -Eeuo pipefail
panels_command() { "$TEST_PYTHON" "$TEST_EXPORTER" panels "$@"; }
compose() { :; }
''' + functions + "\nconfigure_api_panels\n"
            result = subprocess.run([BASH, "-c", harness], input=("2\n2\n" + "\n" * 6 + "0\n").encode(),
                                    capture_output=True,
                                    env=env, timeout=30)
            result.stdout = result.stdout.decode("utf-8")
            result.stderr = result.stderr.decode("utf-8")
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn("修改面板：second", result.stdout)
            self.assertEqual(json.loads(path.read_text(encoding="utf-8")), panels)
            self.assertEqual((path.parent / "xui-second.token").read_text(), "second-token\n")
            self.assertNotIn("second-token", result.stdout + result.stderr)

    def render(self, filename, function, start, stop, directory, center=False):
        script = (ROOT / filename).read_text(encoding="utf-8")
        prelude = script[:script.index(function + "() {")]
        segment = script[script.index(start):script.index(stop)]
        # The Docker helper only runs the real configuration CLI locally.
        # Host-service changes are stubbed; downloads use repository assets.
        stubs = '''
setup_security_traffic_collector() { :; }
docker() {
  while [ "$#" -gt 0 ] && [ "$1" != panels ]; do shift; done
  [ "$#" -gt 0 ] || return 1
  XUI_PANELS_FILE="$STACK_DIR/secrets/xui-panels.json" \
    python "$STACK_DIR/assets/xui_exporter.py" "$@"
}
JOURNAL_PATH_HOST=/var/log/journal
SECURITY_TRAFFIC_VOLUME_LINE=""
SECURITY_TRAFFIC_EXPORTER_CONFIG=""
METRICS_REQUIRED=true
LOKI_URL_HCL="$LOKI_URL"
LOKI_USERNAME_HCL="$LOKI_USERNAME"
LOKI_PASSWORD_HCL=example
METRICS_URL_HCL="$METRICS_URL"
METRICS_USERNAME_HCL="$METRICS_USERNAME"
METRICS_PASSWORD_HCL=example
'''
        if os.name == "nt":
            # Git Bash cannot apply Unix mode bits to all Windows temp dirs.
            stubs += '''
install() {
  if [ "$1" = -d ]; then
    shift; [ "$1" != -m ] || shift 2
    mkdir -p "$@"
  else
    [ "$1" != -m ] || shift 2
    cp "$@"
  fi
}
chmod() { :; }
'''
        env = dict(os.environ, STACK_DIR=pathlib.Path(directory).as_posix(),
                   GLA_ASSET_DIR=ROOT.as_posix(), ENABLE_SECURITY="true",
                   ENABLE_HOST_METRICS="true", ENABLE_GEOIP="false",
                   ENABLE_SECURITY_TRAFFIC="false", SSH_PORT="22",
                   SERVER_NAME="central" if center else "remote",
                   LOKI_URL="https://loki.example.com/loki/api/v1/push",
                   LOKI_USERNAME="test-agent", METRICS_USERNAME="test-agent",
                   METRICS_URL="https://metrics.example.com/api/v1/write",
                   GRAFANA_ADMIN_PASSWORD="test-password",
                   ENABLE_XRAY="true", XRAY_LOG="/obsolete-xray/access.log",
                   XUI_API_URL="https://panel.example.com/base/panel/api/inbounds/list",
                   XUI_API_TOKEN="test-token", XUI_LOCAL_HOST="panel.example.com")
        result = subprocess.run([BASH], input=prelude + stubs + segment,
                                text=True, encoding="utf-8", env=env,
                                capture_output=True, cwd=ROOT, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        stack = pathlib.Path(directory)
        return (stack.joinpath("compose.yaml").read_text(encoding="utf-8"),
                stack.joinpath("alloy/config.alloy").read_text(encoding="utf-8"),
                stack.joinpath(".install.env").read_text(encoding="utf-8"))

    def test_legacy_collector_settings_cannot_enable_xray_collection(self):
        with tempfile.TemporaryDirectory() as directory:
            compose, alloy, settings = self.render(
                "deploy-xray-alloy-collector.sh", "install_collector",
                'note "正在创建采集器文件：$STACK_DIR"',
                'note "正在启动 Alloy 采集器"', directory)
            for obsolete in ("obsolete-xray", "xui-exporter", "xray_access",
                             "xray-access", "XUI_API", "ENABLE_XRAY", "XRAY_LOG"):
                self.assertNotIn(obsolete, compose + alloy + settings)
            for source in ('loki.source.journal "ssh"', 'loki.source.file "fail2ban"',
                           'loki.source.file "ufw"', 'prometheus.scrape "host"'):
                self.assertIn(source, alloy)
            self.assertNotIn("test-token", compose + alloy + settings)

    def test_center_migrates_local_panel_and_generates_only_api_xray_sources(self):
        with tempfile.TemporaryDirectory() as directory:
            compose, alloy, settings = self.render(
                "deploy-xray-grafana-loki-alloy.sh", "install_stack",
                'note "正在创建部署文件：$STACK_DIR"',
                'note "正在启动 Grafana、Loki、VictoriaMetrics 和 Alloy"',
                directory, center=True)
            self.assertIn('loki.source.api "xui_access"', alloy)
            self.assertIn('prometheus.scrape "xui"', alloy)
            self.assertIn('prometheus.scrape "host"', alloy)
            self.assertIn('loki.source.journal "ssh"', alloy)
            self.assertIn("XUI_PANELS_FILE: /run/secrets/xui-panels.json", compose)
            self.assertIn("panel.example.com:host-gateway", compose)
            for obsolete in ("obsolete-xray", 'loki.source.file "xray',
                             'local.file_match "xray', "ENABLE_XRAY", "XRAY_LOG"):
                self.assertNotIn(obsolete, compose + alloy + settings)
            self.assertNotIn("XUI_API_URL", compose + settings)
            self.assertNotIn("test-token", compose + alloy + settings)
            secrets = pathlib.Path(directory) / "secrets"
            panels = json.loads((secrets / "xui-panels.json").read_text(encoding="utf-8"))
            self.assertEqual([p["name"] for p in panels], ["central"])
            self.assertTrue(panels[0]["logs"])
            self.assertEqual((secrets / "xui-central.token").read_text().strip(), "test-token")
            dashboards = pathlib.Path(directory) / "grafana/dashboards"
            for path in dashboards.glob("*.json"):
                json.loads(path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
