#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COLLECTOR="$ROOT/assets/security_traffic_collector.sh"

bash -n "$COLLECTOR"
bash -n "$ROOT/deploy-xray-grafana-loki-alloy.sh"
bash -n "$ROOT/deploy-xray-alloy-collector.sh"
awk '
  /cat >.*MANAGER_PATH.*EOF/ { capture=1; next }
  capture && $0 == "EOF" { exit }
  capture { print }
' "$ROOT/deploy-xray-alloy-collector.sh" | bash -n

grep -Fq 'GLA_SSH_INBOUND' "$COLLECTOR"
grep -Fq 'GLA_UFW_DEFAULT_DENIED' "$COLLECTOR"
grep -Fq 'gla_ssh_inbound_bytes_total' "$COLLECTOR"
grep -Fq 'gla_ufw_default_denied_bytes_total' "$COLLECTOR"
grep -Fq -- '-j RETURN' "$COLLECTOR"

if grep -Eq 'GLA_(SSH_INBOUND|UFW_DEFAULT_DENIED).* -j (ACCEPT|DROP|REJECT)' "$COLLECTOR"; then
  printf 'GLA traffic collector must not alter firewall decisions.\n' >&2
  exit 1
fi

for installer in "$ROOT/deploy-xray-grafana-loki-alloy.sh" "$ROOT/deploy-xray-alloy-collector.sh"; do
  grep -Fq 'ENABLE_SECURITY_TRAFFIC' "$installer"
  grep -Fq 'security_traffic_collector.sh' "$installer"
  grep -Fq 'textfile { directory = "/var/lib/node_exporter/textfile" }' "$installer"
done

printf '安全流量采集器配置验证通过。\n'
