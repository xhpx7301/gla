#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
mkdir -p "$tmp_dir/bin"

cat >"$tmp_dir/bin/docker" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$tmp_dir/bin/install" <<'EOF'
#!/usr/bin/env bash
directories=false
paths=()
while (($#)); do
  case "$1" in
    -d) directories=true; shift ;;
    -m|-o|-g) shift 2 ;;
    *) paths+=("$1"); shift ;;
  esac
done
if [ "$directories" = true ]; then
  mkdir -p "${paths[@]}"
elif ((${#paths[@]} == 2)); then
  cp "${paths[0]}" "${paths[1]}"
fi
EOF
cat >"$tmp_dir/bin/rc-service" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$tmp_dir/bin/rc-update" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
cat >"$tmp_dir/bin/systemctl" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$tmp_dir/bin/docker" "$tmp_dir/bin/install" "$tmp_dir/bin/rc-service" "$tmp_dir/bin/rc-update" "$tmp_dir/bin/systemctl"
PATH="$tmp_dir/bin:$PATH"

# Sourcing exposes the generator without launching its interactive menu.
# shellcheck source=../deploy-xray-alloy-collector.sh
. "$ROOT/deploy-xray-alloy-collector.sh"

STACK_DIR="$tmp_dir/openrc-stack"
SYSTEMD_UNIT_DIR="$tmp_dir/systemd"
OPENRC_INIT_DIR="$tmp_dir/openrc"
HOST_PLATFORM=alpine
ENABLE_SECURITY_TRAFFIC=true
SECURITY_TRAFFIC_BACKEND=nftables-suf
SERVER_NAME=alpine-test
SSH_PORT=2222
mkdir -p "$STACK_DIR/assets" "$SYSTEMD_UNIT_DIR" "$OPENRC_INIT_DIR"
cat >"$STACK_DIR/assets/security_traffic_collector.sh" <<'EOF'
#!/usr/bin/env bash
exit 0
EOF
chmod +x "$STACK_DIR/assets/security_traffic_collector.sh"
setup_security_traffic_collector
bash -n "$STACK_DIR/assets/security_traffic_loop.sh"
bash -n "$OPENRC_INIT_DIR/gla-security-traffic"
grep -Fq 'command_background="yes"' "$OPENRC_INIT_DIR/gla-security-traffic"
grep -Fq 'GLA_FIREWALL_BACKEND=nftables-suf' "$STACK_DIR/security-traffic.env"

STACK_DIR="$tmp_dir/systemd-stack"
HOST_PLATFORM=systemd
SECURITY_TRAFFIC_BACKEND=iptables-ufw
mkdir -p "$STACK_DIR/assets"
cp "$tmp_dir/openrc-stack/assets/security_traffic_collector.sh" "$STACK_DIR/assets/security_traffic_collector.sh"
setup_security_traffic_collector
grep -Fq 'ExecStart=' "$SYSTEMD_UNIT_DIR/gla-security-traffic.service"
grep -Fq 'OnUnitActiveSec=30s' "$SYSTEMD_UNIT_DIR/gla-security-traffic.timer"
grep -Fq 'GLA_FIREWALL_BACKEND=iptables-ufw' "$STACK_DIR/security-traffic.env"

STACK_DIR="$tmp_dir/stack"
INSTALL_SETTINGS_FILE="$STACK_DIR/.install.env"
MANAGER_PATH="$tmp_dir/alloy"
install_manager
bash -n "$MANAGER_PATH"
grep -Fq 'cleanup_security_traffic' "$MANAGER_PATH"
grep -Fq 'rc-service gla-security-traffic stop' "$MANAGER_PATH"
SERVER_NAME=alpine-test
LOKI_URL=https://loki.example.com/loki/api/v1/push
LOKI_USERNAME=alloy-agent
LOKI_PASSWORD=test-password
METRICS_URL=""
METRICS_PASSWORD=""
XUI_API_URL=""
ENABLE_XRAY=false
ENABLE_SECURITY=true
ENABLE_GEOIP=true
ENABLE_SECURITY_TRAFFIC=false
SSH_PORT=2222

prepare_geoip_database() { :; }
prepare_security_log_source() { SECURITY_LOG_PATH=/var/log/messages; }
require_install_prerequisites() {
  HOST_PLATFORM=alpine
  SECURITY_TRAFFIC_VOLUME_LINE=""
  SECURITY_TRAFFIC_EXPORTER_CONFIG=""
  COMPOSE=(true)
}
setup_security_traffic_collector() { :; }
compose() { :; }

(install_collector >/dev/null)

config="$STACK_DIR/alloy/config.alloy"
compose_file="$STACK_DIR/compose.yaml"
settings="$STACK_DIR/.install.env"

grep -Fq 'local.file_match "ssh_alpine"' "$config"
grep -Fq '__path__ = "/host/var/log/messages"' "$config"
grep -Fq 'selector            = "{job=\"ssh-journal\"} !~ \"sshd\""' "$config"
grep -Fq 'local.file_match "fail2ban_alpine"' "$config"
grep -Fq '__path__ = "/host/var/log/fail2ban.log"' "$config"
grep -Fq 'server   = "alpine-test"' "$config"
[ "$(grep -c 'stage.geoip' "$config")" -eq 2 ]
! grep -Fq 'loki.source.journal' "$config"
! grep -Fq '/host/var/log/ufw.log' "$config"

grep -Fq '/var/log:/host/var/log:ro' "$compose_file"
! grep -Fq '/var/log/journal' "$compose_file"
! grep -Fq '/etc/machine-id' "$compose_file"
grep -Fq 'HOST_PLATFORM=alpine' "$settings"

STACK_DIR="$tmp_dir/stack-systemd"
INSTALL_SETTINGS_FILE="$STACK_DIR/.install.env"
prepare_security_log_source() { JOURNAL_PATH_HOST=/run/log/journal; }
require_install_prerequisites() {
  HOST_PLATFORM=systemd
  SECURITY_TRAFFIC_VOLUME_LINE=""
  SECURITY_TRAFFIC_EXPORTER_CONFIG=""
  COMPOSE=(true)
}

(install_collector >/dev/null)

config="$STACK_DIR/alloy/config.alloy"
compose_file="$STACK_DIR/compose.yaml"
grep -Fq 'loki.source.journal "ssh"' "$config"
grep -Fq 'loki.source.journal "sshd"' "$config"
grep -Fq 'local.file_match "fail2ban"' "$config"
grep -Fq '/host/var/log/ufw.log' "$config"
! grep -Fq 'ssh_alpine' "$config"
grep -Fq '/run/log/journal:/var/log/journal:ro' "$compose_file"
grep -Fq '/etc/machine-id:/etc/machine-id:ro' "$compose_file"

printf 'Alpine 与 systemd 采集器配置生成验证通过。\n'
