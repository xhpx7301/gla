#!/usr/bin/env bash
# Exports aggregate firewall byte counters without creating per-IP metric labels.
set -Eeuo pipefail

: "${GLA_TEXTFILE_DIR:?GLA_TEXTFILE_DIR is required}"
: "${GLA_SERVER_NAME:?GLA_SERVER_NAME is required}"
: "${GLA_SSH_PORT:=22}"
: "${GLA_FIREWALL_BACKEND:=iptables-ufw}"

export LC_ALL=C

SSH_CHAIN="GLA_SSH_INBOUND"
UFW_CHAIN="GLA_UFW_DEFAULT_DENIED"
METRICS_FILE="$GLA_TEXTFILE_DIR/security_traffic.prom"

[[ "$GLA_SSH_PORT" =~ ^[0-9]+$ ]] && [ "$GLA_SSH_PORT" -ge 1 ] && [ "$GLA_SSH_PORT" -le 65535 ] || {
  printf 'GLA_SSH_PORT must be between 1 and 65535.\n' >&2
  exit 2
}

metric_escape() {
  printf '%s' "$1" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g' -e 's/\n/\\n/g'
}

chain_bytes() {
  local command_name="$1" chain_name="$2"
  "$command_name" -w -t filter -L "$chain_name" -v -x -n 2>/dev/null |
    awk '$3 == "RETURN" { print $2; found = 1; exit } END { if (!found) print 0 }'
}

ensure_return_chain() {
  local command_name="$1" chain_name="$2"
  "$command_name" -w -t filter -N "$chain_name" 2>/dev/null || true
  "$command_name" -w -t filter -C "$chain_name" -j RETURN >/dev/null 2>&1 ||
    "$command_name" -w -t filter -A "$chain_name" -j RETURN
}

ensure_ssh_counter() {
  local command_name="$1"
  ensure_return_chain "$command_name" "$SSH_CHAIN"
  "$command_name" -w -t filter -C INPUT -p tcp --dport "$GLA_SSH_PORT" -j "$SSH_CHAIN" >/dev/null 2>&1 ||
    "$command_name" -w -t filter -I INPUT 1 -p tcp --dport "$GLA_SSH_PORT" -j "$SSH_CHAIN"
}

ensure_ufw_default_counter() {
  local command_name="$1" policy
  policy="$("$command_name" -w -S INPUT 2>/dev/null | awk '$1 == "-P" && $2 == "INPUT" { print $3; exit }')"
  case "$policy" in
    DROP|REJECT) ;;
    *) return 1 ;;
  esac

  ensure_return_chain "$command_name" "$UFW_CHAIN"
  "$command_name" -w -t filter -C INPUT -j "$UFW_CHAIN" >/dev/null 2>&1 ||
    "$command_name" -w -t filter -A INPUT -j "$UFW_CHAIN"
}

cleanup_family() {
  local command_name="$1"
  command -v "$command_name" >/dev/null 2>&1 || return 0

  while "$command_name" -w -t filter -D INPUT -p tcp --dport "$GLA_SSH_PORT" -j "$SSH_CHAIN" >/dev/null 2>&1; do :; done
  while "$command_name" -w -t filter -D INPUT -j "$UFW_CHAIN" >/dev/null 2>&1; do :; done
  "$command_name" -w -t filter -F "$SSH_CHAIN" >/dev/null 2>&1 || true
  "$command_name" -w -t filter -X "$SSH_CHAIN" >/dev/null 2>&1 || true
  "$command_name" -w -t filter -F "$UFW_CHAIN" >/dev/null 2>&1 || true
  "$command_name" -w -t filter -X "$UFW_CHAIN" >/dev/null 2>&1 || true
}

nft_rule_handles() {
  local family="$1" table_name="$2" chain_name="$3" marker="$4"
  nft -a list chain "$family" "$table_name" "$chain_name" 2>/dev/null |
    awk -v wanted="comment \"$marker\"" '
      index($0, wanted) {
        for (i = 1; i <= NF; i++) {
          if ($i == "handle") print $(i + 1)
        }
      }
    '
}

delete_nft_rules() {
  local family="$1" table_name="$2" chain_name="$3" marker="$4" handle
  while IFS= read -r handle; do
    [ -n "$handle" ] || continue
    nft delete rule "$family" "$table_name" "$chain_name" handle "$handle" >/dev/null 2>&1 || true
  done < <(nft_rule_handles "$family" "$table_name" "$chain_name" "$marker")
}

nft_counter_bytes() {
  local family="$1" table_name="$2" chain_name="$3" marker="$4"
  nft -a list chain "$family" "$table_name" "$chain_name" 2>/dev/null |
    awk -v wanted="comment \"$marker\"" '
      index($0, wanted) {
        for (i = 1; i <= NF; i++) {
          if ($i == "bytes") { print $(i + 1); found = 1; exit }
        }
      }
      END { if (!found) print 0 }
    '
}

ensure_nft_traffic_table() {
  if ! nft list table inet gla_traffic >/dev/null 2>&1; then
    nft add table inet gla_traffic
  fi
  if ! nft list chain inet gla_traffic input >/dev/null 2>&1; then
    nft add chain inet gla_traffic input '{ type filter hook input priority -110; policy accept; }'
  fi
}

ensure_nft_ssh_counter() {
  local marker="GLA SSH inbound" current
  current="$(nft -a list chain inet gla_traffic input 2>/dev/null || true)"
  if printf '%s\n' "$current" | grep -F "comment \"$marker\"" | grep -Fq "tcp dport $GLA_SSH_PORT"; then
    return
  fi
  delete_nft_rules inet gla_traffic input "$marker"
  nft add rule inet gla_traffic input tcp dport "$GLA_SSH_PORT" counter comment "$marker"
}

ensure_suf_default_denied_counter() {
  local marker="GLA SUF default denied" marker_handle last_handle
  nft list chain inet suf input >/dev/null 2>&1 || return 1
  marker_handle="$(nft_rule_handles inet suf input "$marker" | tail -n 1)"
  last_handle="$(nft -a list chain inet suf input 2>/dev/null | awk '$1 != "chain" { for (i = 1; i <= NF; i++) if ($i == "handle") handle = $(i + 1) } END { print handle }')"
  if [ -n "$marker_handle" ] && [ "$marker_handle" = "$last_handle" ]; then
    return
  fi
  delete_nft_rules inet suf input "$marker"
  nft add rule inet suf input counter comment "$marker"
}

cleanup_nftables() {
  delete_nft_rules inet suf input "GLA SUF default denied"
  nft delete table inet gla_traffic >/dev/null 2>&1 || true
}

if [ "${1:-}" = "--cleanup" ]; then
  case "$GLA_FIREWALL_BACKEND" in
    nftables-suf) cleanup_nftables ;;
    *)
      cleanup_family iptables
      cleanup_family ip6tables
      ;;
  esac
  rm -f "$METRICS_FILE"
  exit 0
fi

install -d -m 0750 "$GLA_TEXTFILE_DIR"
temporary_file="$(mktemp "$GLA_TEXTFILE_DIR/.security_traffic.prom.XXXXXX")"
trap 'rm -f "$temporary_file"' EXIT

server_label="$(metric_escape "$GLA_SERVER_NAME")"
cat >"$temporary_file" <<EOF
# HELP gla_ssh_inbound_bytes_total Bytes received by the SSH listening port, including normal sessions and scans.
# TYPE gla_ssh_inbound_bytes_total counter
# HELP gla_ufw_default_denied_bytes_total Bytes that reached the host firewall default INPUT deny policy. The metric name is retained for compatibility.
# TYPE gla_ufw_default_denied_bytes_total counter
# HELP gla_security_traffic_collector_up Whether the host firewall traffic collector completed successfully.
# TYPE gla_security_traffic_collector_up gauge
EOF

if [ "$GLA_FIREWALL_BACKEND" = nftables-suf ]; then
  command -v nft >/dev/null 2>&1
  ensure_nft_traffic_table
  ensure_nft_ssh_counter
  ensure_suf_default_denied_counter
  ssh_bytes="$(nft_counter_bytes inet gla_traffic input "GLA SSH inbound")"
  denied_bytes="$(nft_counter_bytes inet suf input "GLA SUF default denied")"
  printf 'gla_ssh_inbound_bytes_total{server="%s",family="inet"} %s\n' "$server_label" "$ssh_bytes" >>"$temporary_file"
  # Keep the established metric name so existing dashboards continue to work.
  printf 'gla_ufw_default_denied_bytes_total{server="%s",family="inet"} %s\n' "$server_label" "$denied_bytes" >>"$temporary_file"
else
  for family in ipv4 ipv6; do
    if [ "$family" = ipv4 ]; then
      command_name=iptables
    else
      command_name=ip6tables
    fi

    command -v "$command_name" >/dev/null 2>&1 || continue
    ensure_ssh_counter "$command_name"
    ssh_bytes="$(chain_bytes "$command_name" "$SSH_CHAIN")"
    printf 'gla_ssh_inbound_bytes_total{server="%s",family="%s"} %s\n' "$server_label" "$family" "$ssh_bytes" >>"$temporary_file"

    if ensure_ufw_default_counter "$command_name"; then
      denied_bytes="$(chain_bytes "$command_name" "$UFW_CHAIN")"
      printf 'gla_ufw_default_denied_bytes_total{server="%s",family="%s"} %s\n' "$server_label" "$family" "$denied_bytes" >>"$temporary_file"
    fi
  done
fi

printf 'gla_security_traffic_collector_up{server="%s"} 1\n' "$server_label" >>"$temporary_file"
chmod 0644 "$temporary_file"
mv "$temporary_file" "$METRICS_FILE"
