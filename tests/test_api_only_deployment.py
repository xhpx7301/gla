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
    def test_center_connects_running_npm_to_shared_network_idempotently(self):
        script = (ROOT / "deploy-xray-grafana-loki-alloy.sh").read_text(encoding="utf-8")
        function = script[script.index("connect_npm_to_shared_network() {"):script.index("install_stack() {")]
        harness = r'''set -Eeuo pipefail
NPM_NETWORK=shared-proxy
connected=0
connect_calls=0
docker() {
  if [ "$1" = ps ]; then
    printf 'nginx-proxy-manager\tjc21/nginx-proxy-manager:latest\n'
  elif [ "$1" = network ] && [ "$2" = inspect ]; then
    if [ "$connected" -eq 1 ] && [[ "$*" == *--format* ]]; then
      printf 'nginx-proxy-manager\n'
    fi
  elif [ "$1" = network ] && [ "$2" = connect ]; then
    connected=1
    connect_calls=$((connect_calls + 1))
  else
    return 1
  fi
}
''' + function + '''
connect_npm_to_shared_network
connect_npm_to_shared_network
[ "$connect_calls" -eq 1 ]
'''
        result = subprocess.run([BASH, "-c", harness], text=True, encoding="utf-8",
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("已接入共享网络", result.stdout)
        self.assertIn("已连接共享网络", result.stdout)

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

    def render(self, filename, function, start, stop, directory, center=False, ipv6=False):
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
                   XUI_API_TOKEN="test-token", XUI_LOCAL_HOST="panel.example.com",
                   XUI_ENABLE_IPV6="true" if ipv6 else "false",
                   XUI_IPV6_SUBNET="fd12:3456:789a:0::/64" if ipv6 else "")
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
            self.assertIn("XUI_TELEGRAM_CONFIG_FILE: /run/secrets/telegram-config.json", compose)
            self.assertIn("XUI_WEB_ADMIN_CONFIG_FILE: /run/secrets/web-admin.json", compose)
            self.assertIn("./secrets:/run/secrets", compose)
            self.assertIn("panel.example.com:host-gateway", compose)
            self.assertNotIn("enable_ipv6:", compose)
            self.assertIn("XUI_ENABLE_IPV6=false", settings)
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

    def test_ipv6_egress_is_attached_only_to_exporter_without_replacing_existing_networks(self):
        with tempfile.TemporaryDirectory() as directory:
            compose, alloy, settings = self.render(
                "deploy-xray-grafana-loki-alloy.sh", "install_stack",
                'note "正在创建部署文件：$STACK_DIR"',
                'note "正在启动 Grafana、Loki、VictoriaMetrics 和 Alloy"',
                directory, center=True, ipv6=True)
            exporter = compose.split("  xui-exporter:\n", 1)[1].split("\nvolumes:", 1)[0]
            self.assertIn("      - default\n      - npm\n      - xui_ipv6", exporter)
            self.assertEqual(compose.count("      - xui_ipv6"), 1)
            self.assertIn("  xui_ipv6:\n    driver: bridge\n    enable_ipv6: true", compose)
            self.assertIn("    external: true", compose)
            self.assertIn("XUI_ENABLE_IPV6=true", settings)
            self.assertIn("XUI_IPV6_SUBNET=fd12:3456:789a:0::/64", settings)
            self.assertIn("        - subnet: fd12:3456:789a:0::/64", compose)
            self.assertNotIn("network_mode: host", compose)

    def test_ipv6_checks_docker_server_version_before_deployment(self):
        script = (ROOT / "deploy-xray-grafana-loki-alloy.sh").read_text(encoding="utf-8")
        function = script[script.index("require_install_prerequisites() {"):
                          script.index("setup_security_traffic_collector() {")]
        harness = r'''set -Eeuo pipefail
die() { printf '%s\n' "$*" >&2; exit 1; }
id() { printf '0\n'; }
ENABLE_GEOIP=false
ENABLE_SECURITY_TRAFFIC=false
SSH_PORT=22
SERVER_NAME=central
XUI_API_URL=''
XUI_LOCAL_HOST=''
docker() {
  if [ "$1" = version ]; then printf '%s\n' "$TEST_DOCKER_VERSION"; fi
  if [ "$1" = info ]; then printf '%s\n' "$TEST_EXPERIMENTAL"; fi
}
''' + function + '\nrequire_install_prerequisites\nprintf "subnet=%s\\n" "$XUI_IPV6_SUBNET"\n'
        for enabled, version, experimental, ok in (("true", "27.0.0", "false", True),
                                     ("true", "29.0.1", "false", True),
                                     ("true", "26.1.5+dfsg1", "true", True),
                                     ("true", "26.1.5+dfsg1", "false", False),
                                     ("true", "25.0.5", "true", False),
                                     ("false", "26.1.5", "false", True),
                                     ("invalid", "29.0.1", "false", False)):
            with self.subTest(enabled=enabled, version=version):
                result = subprocess.run([BASH, "-c", harness], text=True, encoding="utf-8",
                                        capture_output=True, timeout=10,
                                        env=dict(os.environ, XUI_ENABLE_IPV6=enabled,
                                                 XUI_IPV6_SUBNET="", TEST_DOCKER_VERSION=version,
                                                 TEST_EXPERIMENTAL=experimental))
                self.assertEqual(result.returncode == 0, ok, result.stdout + result.stderr)
                if enabled == "true" and ok:
                    self.assertRegex(result.stdout, r"subnet=fd[0-9a-f]{2}:[0-9a-f]{4}:[0-9a-f]{4}:0::/64")
                if enabled == "true" and version.startswith("25"):
                    self.assertIn("Docker Engine 26", result.stderr)
                if enabled == "true" and version.startswith("26") and not ok:
                    self.assertIn("experimental 和 ip6tables", result.stderr)

    def test_updates_preserve_ipv6_setting_and_accept_explicit_override(self):
        script = (ROOT / "deploy-xray-grafana-loki-alloy.sh").read_text(encoding="utf-8")
        function = script[script.index("update_script_and_deploy() {"):
                          script.index("panels_command() {")]
        with tempfile.TemporaryDirectory() as directory:
            settings = pathlib.Path(directory) / "settings.env"
            for saved, override, expected in (("true", None, "true"), ("false", "true", "true"),
                                               ("true", "false", "false")):
                with self.subTest(saved=saved, override=override):
                    settings.write_text(f"XUI_ENABLE_IPV6={saved}\nXUI_IPV6_SUBNET=fd12:3456:789a:0::/64\n", encoding="utf-8")
                    env = dict(os.environ, INSTALL_SETTINGS_FILE=settings.as_posix())
                    env.pop("XUI_ENABLE_IPV6", None)
                    env.pop("XUI_IPV6_SUBNET", None)
                    if override is not None:
                        env["XUI_ENABLE_IPV6"] = override
                    harness = '''set -Eeuo pipefail
download_installer() { printf 'printf "ipv6=%%s subnet=%%s\\n" "$XUI_ENABLE_IPV6" "$XUI_IPV6_SUBNET"\n'; }
''' + function + "\nupdate_script_and_deploy\n"
                    result = subprocess.run([BASH, "-c", harness], text=True, encoding="utf-8",
                                            capture_output=True, timeout=10, env=env)
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertIn(f"ipv6={expected}", result.stdout)
                    self.assertIn("subnet=fd12:3456:789a:0::/64", result.stdout)

    def test_generated_compose_configures_log_rotation_and_safe_image_updates(self):
        with tempfile.TemporaryDirectory() as center_dir, tempfile.TemporaryDirectory() as collector_dir:
            center_compose, _, _ = self.render(
                "deploy-xray-grafana-loki-alloy.sh", "install_stack",
                'note "正在创建部署文件：$STACK_DIR"',
                'note "正在启动 Grafana、Loki、VictoriaMetrics 和 Alloy"',
                center_dir, center=True)
            collector_compose, _, _ = self.render(
                "deploy-xray-alloy-collector.sh", "install_collector",
                'note "正在创建采集器文件：$STACK_DIR"',
                'note "正在启动 Alloy 采集器"',
                collector_dir, center=False)
            self.assertEqual(center_compose.count("driver: json-file"), 5)
            self.assertEqual(center_compose.count('max-size: "10m"'), 5)
            self.assertEqual(collector_compose.count("driver: json-file"), 1)
            self.assertEqual(collector_compose.count('max-size: "10m"'), 1)
        central_script = (ROOT / "deploy-xray-grafana-loki-alloy.sh").read_text(encoding="utf-8")
        self.assertIn("compose pull grafana loki victoriametrics alloy", central_script)
        self.assertIn("compose build --pull xui-exporter", central_script)


if __name__ == "__main__":
    unittest.main()
