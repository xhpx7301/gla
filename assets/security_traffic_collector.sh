#!/usr/bin/env bash
# Exports aggregate firewall byte counters without creating per-IP metric labels.
set -Eeuo pipefail

: "${GLA_TEXTFILE_DIR:?GLA_TEXTFILE_DIR is required}"
: "${GLA_SERVER_NAME:?GLA_SERVER_NAME is required}"
: "${GLA_SSH_PORT:=22}"

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

if [ "${1:-}" = "--cleanup" ]; then
  cleanup_family iptables
  cleanup_family ip6tables
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
# HELP gla_ufw_default_denied_bytes_total Bytes that reached the UFW default INPUT deny policy.
# TYPE gla_ufw_default_denied_bytes_total counter
# HELP gla_security_traffic_collector_up Whether the host firewall traffic collector completed successfully.
# TYPE gla_security_traffic_collector_up gauge
EOF

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

printf 'gla_security_traffic_collector_up{server="%s"} 1\n' "$server_label" >>"$temporary_file"
chmod 0644 "$temporary_file"
mv "$temporary_file" "$METRICS_FILE"
