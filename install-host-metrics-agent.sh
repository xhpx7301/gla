#!/usr/bin/env bash
set -Eeuo pipefail

ACTION="${1:-install}"
SERVER_NAME="${SERVER_NAME:-}"
HOST_METRICS_URL="${HOST_METRICS_URL:-}"
HOST_METRICS_KEY="${HOST_METRICS_KEY:-}"
METRICS_INTERVAL="${METRICS_INTERVAL:-30}"
CONFIG_DIR=/etc/gla
CONFIG_FILE="$CONFIG_DIR/host-metrics.env"
SSH_ENV_FILE="$CONFIG_DIR/ssh-login.env"
AGENT_FILE=/usr/local/sbin/gla-host-metrics
SYSTEMD_UNIT=/etc/systemd/system/gla-host-metrics.service
OPENRC_INIT=/etc/init.d/gla-host-metrics

die() { printf '错误：%s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  die "请使用 root 执行。"
fi

case "$ACTION" in
  install)
    if [ -r "$SSH_ENV_FILE" ]; then
      # shellcheck disable=SC1090
      . "$SSH_ENV_FILE"
      [ -n "$SERVER_NAME" ] || SERVER_NAME="${GLA_SERVER_NAME:-}"
      [ -n "$HOST_METRICS_KEY" ] || HOST_METRICS_KEY="${GLA_SSH_LOGIN_KEY:-}"
      if [ -z "$HOST_METRICS_URL" ] && [ -n "${GLA_SSH_LOGIN_URL:-}" ]; then
        HOST_METRICS_URL="${GLA_SSH_LOGIN_URL%/api/ssh-login}/api/host-metrics"
      fi
    fi

    [[ "$SERVER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die "请设置有效的 SERVER_NAME。"
    [[ "$HOST_METRICS_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/api/host-metrics$ ]] || die "HOST_METRICS_URL 必须是 HTTPS 地址，并以 /api/host-metrics 结尾。"
    [[ "$HOST_METRICS_KEY" =~ ^[A-Fa-f0-9]{64}$ ]] || die "HOST_METRICS_KEY 必须是 64 位十六进制密钥。"
    [[ "$METRICS_INTERVAL" =~ ^[0-9]+$ ]] && [ "$METRICS_INTERVAL" -ge 10 ] && [ "$METRICS_INTERVAL" -le 300 ] || die "METRICS_INTERVAL 必须在 10 到 300 秒之间。"
    command -v curl >/dev/null 2>&1 || die "需要 curl。"
    command -v awk >/dev/null 2>&1 || die "需要 awk。"

    install -d -o root -g root -m 0700 "$CONFIG_DIR"
    printf 'GLA_SERVER_NAME=%q\nGLA_HOST_METRICS_URL=%q\nGLA_HOST_METRICS_KEY=%q\nGLA_METRICS_INTERVAL=%q\n' \
      "$SERVER_NAME" "$HOST_METRICS_URL" "$HOST_METRICS_KEY" "$METRICS_INTERVAL" >"$CONFIG_FILE"
    chmod 0600 "$CONFIG_FILE"

    cat >"$AGENT_FILE" <<'AGENT'
#!/bin/sh
set -u

ENV_FILE="/etc/gla/host-metrics.env"
[ -r "$ENV_FILE" ] || exit 1
# shellcheck disable=SC1090
. "$ENV_FILE"

INTERVAL="${GLA_METRICS_INTERVAL:-30}"
CLK_TCK="$(getconf CLK_TCK 2>/dev/null || echo 100)"
[ "$CLK_TCK" -ge 1 ] 2>/dev/null || CLK_TCK=100
CORES="$(grep -c '^processor' /proc/cpuinfo 2>/dev/null || echo 1)"
[ "$CORES" -ge 1 ] 2>/dev/null || CORES=1

push_once() {
  net_stats="$(awk '
    NR > 2 {
      line = $0
      sub(/:/, " ", line)
      n = split(line, f, /[ \t]+/)
      idx = (f[1] == "") ? 2 : 1
      iface = f[idx]
      if (iface == "" || iface == "lo" || iface ~ /^(docker|veth|br-|virbr|tun|tap|wg|tailscale)/) next
      rx_b += f[idx + 1]
      rx_p += f[idx + 2]
      tx_b += f[idx + 9]
      tx_p += f[idx + 10]
    }
    END { printf "%.0f %.0f %.0f %.0f", rx_b, rx_p, tx_b, tx_p }
  ' /proc/net/dev 2>/dev/null)"
  set -- $net_stats
  rx_bytes="${1:-0}"
  rx_packets="${2:-0}"
  tx_bytes="${3:-0}"
  tx_packets="${4:-0}"

  cpu_stats="$(awk -v div="$((CLK_TCK * CORES))" '
    $1 == "cpu" {
      idle = $5 + $6
      softirq = $8 + 0
      printf "%.6f %.6f", idle / div, softirq / div
      exit
    }
  ' /proc/stat 2>/dev/null)"
  set -- $cpu_stats
  cpu_idle="${1:-0}"
  cpu_softirq="${2:-0}"

  mem_stats="$(awk '
    $1 == "MemTotal:" { total = $2 * 1024 }
    $1 == "MemAvailable:" { avail = $2 * 1024; has_avail = 1 }
    $1 == "MemFree:" { mfree = $2 * 1024 }
    $1 == "Buffers:" { buffers = $2 * 1024 }
    $1 == "Cached:" { cached = $2 * 1024 }
    END {
      if (!has_avail) avail = mfree + buffers + cached
      printf "%.0f %.0f", total, avail
    }
  ' /proc/meminfo 2>/dev/null)"
  set -- $mem_stats
  mem_total="${1:-0}"
  mem_avail="${2:-0}"

  ufw_arg=""
  if command -v iptables >/dev/null 2>&1; then
    ufw_bytes="$(iptables -w -t filter -L GLA_UFW_DEFAULT_DENIED -v -x -n 2>/dev/null | awk '$3 == "RETURN" { print $2; found=1; exit } END { if (!found) print "" }')"
    if [ -n "$ufw_bytes" ]; then
      ufw_arg="$ufw_bytes"
    fi
  fi

  escaped_key="$(printf '%s' "$GLA_HOST_METRICS_KEY" | sed -e 's/\\/\\\\/g' -e 's/"/\\"/g')"
  if [ -n "$ufw_arg" ]; then
    printf 'header = "X-GLA-Key: %s"\n' "$escaped_key" | \
      curl --config - --silent --show-error --fail --connect-timeout 3 --max-time 8 \
        --data-urlencode "server=$GLA_SERVER_NAME" \
        --data-urlencode "rx_bytes=$rx_bytes" \
        --data-urlencode "rx_packets=$rx_packets" \
        --data-urlencode "tx_bytes=$tx_bytes" \
        --data-urlencode "tx_packets=$tx_packets" \
        --data-urlencode "cpu_idle_seconds=$cpu_idle" \
        --data-urlencode "cpu_softirq_seconds=$cpu_softirq" \
        --data-urlencode "mem_total_bytes=$mem_total" \
        --data-urlencode "mem_available_bytes=$mem_avail" \
        --data-urlencode "ufw_denied_bytes=$ufw_arg" \
        "$GLA_HOST_METRICS_URL" >/dev/null 2>&1
  else
    printf 'header = "X-GLA-Key: %s"\n' "$escaped_key" | \
      curl --config - --silent --show-error --fail --connect-timeout 3 --max-time 8 \
        --data-urlencode "server=$GLA_SERVER_NAME" \
        --data-urlencode "rx_bytes=$rx_bytes" \
        --data-urlencode "rx_packets=$rx_packets" \
        --data-urlencode "tx_bytes=$tx_bytes" \
        --data-urlencode "tx_packets=$tx_packets" \
        --data-urlencode "cpu_idle_seconds=$cpu_idle" \
        --data-urlencode "cpu_softirq_seconds=$cpu_softirq" \
        --data-urlencode "mem_total_bytes=$mem_total" \
        --data-urlencode "mem_available_bytes=$mem_avail" \
        "$GLA_HOST_METRICS_URL" >/dev/null 2>&1
  fi
}

if [ "${1:-}" = "--once" ]; then
  push_once
  exit $?
fi

while true; do
  push_once || true
  sleep "$INTERVAL"
done
AGENT
    chmod 0750 "$AGENT_FILE"

    if command -v systemctl >/dev/null 2>&1; then
      cat >"$SYSTEMD_UNIT" <<EOF
[Unit]
Description=GLA Lightweight Host Metrics Push Agent
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart=$AGENT_FILE
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
      systemctl daemon-reload
      systemctl enable --now gla-host-metrics.service
      systemctl restart gla-host-metrics.service
    elif command -v rc-service >/dev/null 2>&1; then
      cat >"$OPENRC_INIT" <<EOF
#!/sbin/openrc-run
description="GLA Lightweight Host Metrics Push Agent"
command="$AGENT_FILE"
command_background=true
pidfile="/run/gla-host-metrics.pid"

depend() {
  need net
}
EOF
      chmod 0755 "$OPENRC_INIT"
      rc-update add gla-host-metrics default >/dev/null 2>&1 || true
      rc-service gla-host-metrics restart
    else
      die "未检测到 systemd 或 OpenRC，无法注册常驻服务。"
    fi

    if "$AGENT_FILE" --once; then
      printf 'GLA 轻量化主机指标采集已启用：%s（首次上报成功）\n' "$SERVER_NAME"
    else
      printf '警告：服务已启动，但首次上报 %s 失败；请检查中心端是否已更新或网络连通性。\n' "$HOST_METRICS_URL" >&2
    fi
    ;;

  uninstall)
    if command -v systemctl >/dev/null 2>&1; then
      systemctl disable --now gla-host-metrics.service 2>/dev/null || true
      rm -f "$SYSTEMD_UNIT"
      systemctl daemon-reload 2>/dev/null || true
    fi
    if command -v rc-service >/dev/null 2>&1; then
      rc-service gla-host-metrics stop 2>/dev/null || true
      rc-update del gla-host-metrics default >/dev/null 2>&1 || true
      rm -f "$OPENRC_INIT"
    fi
    rm -f "$AGENT_FILE" "$CONFIG_FILE"
    printf 'GLA 轻量化主机指标采集已移除。\n'
    ;;

  *)
    die "用法：$0 [install|uninstall]"
    ;;
esac
