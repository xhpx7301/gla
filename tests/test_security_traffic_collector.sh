#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
COLLECTOR="$ROOT/assets/security_traffic_collector.sh"

bash -n "$COLLECTOR"
bash -n "$ROOT/deploy-xray-grafana-loki-alloy.sh"
bash -n "$ROOT/deploy-xray-alloy-collector.sh"

grep -Fq 'GLA_SSH_INBOUND' "$COLLECTOR"
grep -Fq 'GLA_UFW_DEFAULT_DENIED' "$COLLECTOR"
grep -Fq 'gla_ssh_inbound_bytes_total' "$COLLECTOR"
grep -Fq 'gla_ufw_default_denied_bytes_total' "$COLLECTOR"
grep -Fq -- '-j RETURN' "$COLLECTOR"

if grep -Eq 'GLA_(SSH_INBOUND|UFW_DEFAULT_DENIED).* -j (ACCEPT|DROP|REJECT)' "$COLLECTOR"; then
  printf 'GLA traffic collector must not alter firewall decisions.\n' >&2
  exit 1
fi

if grep -Eq 'nft add rule .*GLA.* (accept|drop|reject)' "$COLLECTOR"; then
  printf 'GLA nftables counters must not alter firewall decisions.\n' >&2
  exit 1
fi

for installer in "$ROOT/deploy-xray-grafana-loki-alloy.sh" "$ROOT/deploy-xray-alloy-collector.sh"; do
  grep -Fq 'ENABLE_SECURITY_TRAFFIC' "$installer"
  grep -Fq 'security_traffic_collector.sh' "$installer"
  grep -Fq 'textfile { directory = "/var/lib/node_exporter/textfile" }' "$installer"
done

grep -Fq 'HOST_PLATFORM=alpine' "$ROOT/deploy-xray-alloy-collector.sh"
grep -Fq 'rc-service gla-security-traffic restart' "$ROOT/deploy-xray-alloy-collector.sh"
grep -Fq 'cleanup_security_traffic' "$ROOT/deploy-xray-alloy-collector.sh"
grep -Fq '__path__ = "/host$SECURITY_LOG_PATH"' "$ROOT/deploy-xray-alloy-collector.sh"
grep -Fq 'nftables-suf' "$COLLECTOR"

# Configuration redeploys reuse local images; menu option 8 updates Alloy only.
! grep -Fxq 'compose pull' "$ROOT/deploy-xray-alloy-collector.sh"
grep -Fq 'compose pull alloy' "$ROOT/deploy-xray-alloy-collector.sh"
grep -Fq 'compose up -d --no-deps alloy' "$ROOT/deploy-xray-alloy-collector.sh"

tmp_dir="$(mktemp -d)"
trap 'rm -rf "$tmp_dir"' EXIT
mkdir -p "$tmp_dir/bin" "$tmp_dir/textfile"
cat >"$tmp_dir/bin/install" <<'EOF'
#!/usr/bin/env bash
target="${@: -1}"
mkdir -p "$target"
EOF
cat >"$tmp_dir/bin/nft" <<'EOF'
#!/usr/bin/env bash
case "$*" in
  'list table inet gla_traffic') exit 0 ;;
  'list chain inet gla_traffic input') exit 0 ;;
  '-a list chain inet gla_traffic input')
    printf '%s\n' 'tcp dport 2222 counter packets 3 bytes 1200 comment "GLA SSH inbound" # handle 7'
    ;;
  'list chain inet suf input') exit 0 ;;
  '-a list chain inet suf input')
    printf '%s\n' 'tcp dport 2222 accept comment "SUF SSH" # handle 4'
    printf '%s\n' 'counter packets 9 bytes 4096 comment "GLA SUF default denied" # handle 8'
    ;;
  *)
    printf 'Unexpected nft command: %s\n' "$*" >&2
    exit 1
    ;;
esac
EOF
chmod +x "$tmp_dir/bin/install" "$tmp_dir/bin/nft"

PATH="$tmp_dir/bin:$PATH" \
GLA_TEXTFILE_DIR="$tmp_dir/textfile" \
GLA_SERVER_NAME="alpine-test" \
GLA_SSH_PORT=2222 \
GLA_FIREWALL_BACKEND=nftables-suf \
  "$COLLECTOR"

metrics_file="$tmp_dir/textfile/security_traffic.prom"
grep -Fq 'gla_ssh_inbound_bytes_total{server="alpine-test",family="inet"} 1200' "$metrics_file"
grep -Fq 'gla_ufw_default_denied_bytes_total{server="alpine-test",family="inet"} 4096' "$metrics_file"
grep -Fq 'gla_security_traffic_collector_up{server="alpine-test"} 1' "$metrics_file"

printf '安全流量采集器配置验证通过。\n'
