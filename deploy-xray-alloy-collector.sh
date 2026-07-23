#!/usr/bin/env bash
# Deploys a modular Alloy collector for security logs, host metrics, and Xray.
set -Eeuo pipefail

STACK_DIR="${STACK_DIR:-/opt/xray-alloy-collector}"
XRAY_LOG="${XRAY_LOG:-/var/log/x-ui/access.log}"
ENABLE_XRAY="${ENABLE_XRAY:-auto}"
ENABLE_SECURITY="${ENABLE_SECURITY:-true}"
ENABLE_GEOIP="${ENABLE_GEOIP:-auto}"
ENABLE_HOST_METRICS="${ENABLE_HOST_METRICS:-auto}"
ENABLE_SECURITY_TRAFFIC="${ENABLE_SECURITY_TRAFFIC:-auto}"
SSH_PORT="${SSH_PORT:-}"
SERVER_NAME="${SERVER_NAME:-}"
LOKI_URL="${LOKI_URL:-}"
LOKI_USERNAME="${LOKI_USERNAME:-alloy-agent}"
LOKI_PASSWORD="${LOKI_PASSWORD:-}"
METRICS_URL="${METRICS_URL:-}"
METRICS_USERNAME="${METRICS_USERNAME:-alloy-agent}"
METRICS_PASSWORD="${METRICS_PASSWORD:-}"
XUI_API_URL="${XUI_API_URL:-}"
XUI_API_TOKEN="${XUI_API_TOKEN:-}"
ALLOY_IMAGE="${ALLOY_IMAGE:-grafana/alloy:latest}"
ASSET_BASE_URL="${ASSET_BASE_URL:-https://raw.githubusercontent.com/xhpx7301/gla/main}"
MANAGER_PATH="/usr/local/bin/alloy"
INSTALL_SETTINGS_FILE="$STACK_DIR/.install.env"
GEOIP_DB_PATH="${GEOIP_DB_PATH:-$STACK_DIR/geoip/GeoLite2-City.mmdb}"
GEOIP_MIRROR_URL="${GEOIP_MIRROR_URL:-https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-City.mmdb}"

die() { printf '错误: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }
hcl_escape() { sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
compose() { "${COMPOSE[@]}" "$@"; }

write_install_settings() {
  {
    printf 'STACK_DIR=%q\n' "$STACK_DIR"
    printf 'XRAY_LOG=%q\n' "$XRAY_LOG"
    printf 'ENABLE_XRAY=%q\n' "$ENABLE_XRAY"
    printf 'ENABLE_SECURITY=%q\n' "$ENABLE_SECURITY"
    printf 'ENABLE_GEOIP=%q\n' "$ENABLE_GEOIP"
    printf 'ENABLE_HOST_METRICS=%q\n' "$ENABLE_HOST_METRICS"
    printf 'ENABLE_SECURITY_TRAFFIC=%q\n' "$ENABLE_SECURITY_TRAFFIC"
    printf 'SSH_PORT=%q\n' "$SSH_PORT"
    printf 'GEOIP_DB_PATH=%q\n' "$GEOIP_DB_PATH"
    printf 'GEOIP_MIRROR_URL=%q\n' "$GEOIP_MIRROR_URL"
    printf 'SERVER_NAME=%q\n' "$SERVER_NAME"
    printf 'LOKI_URL=%q\n' "$LOKI_URL"
    printf 'LOKI_USERNAME=%q\n' "$LOKI_USERNAME"
    printf 'METRICS_URL=%q\n' "$METRICS_URL"
    printf 'METRICS_USERNAME=%q\n' "$METRICS_USERNAME"
    printf 'XUI_API_URL=%q\n' "$XUI_API_URL"
    printf 'ALLOY_IMAGE=%q\n' "$ALLOY_IMAGE"
  } >"$INSTALL_SETTINGS_FILE"
  chmod 0600 "$INSTALL_SETTINGS_FILE"
}

require_install_prerequisites() {
  [ "$(id -u)" -eq 0 ] || die "请使用 root 执行：sudo bash $0"
  case "$ENABLE_XRAY" in
    auto) [ -r "$XRAY_LOG" ] && ENABLE_XRAY=true || ENABLE_XRAY=false ;;
    true) [ -r "$XRAY_LOG" ] || die "已要求采集 Xray，但无法读取日志：$XRAY_LOG" ;;
    false) ;;
    *) die "ENABLE_XRAY 只能是 auto、true 或 false。" ;;
  esac
  case "$ENABLE_SECURITY" in true|false) ;; *) die "ENABLE_SECURITY 只能是 true 或 false。" ;; esac
  case "$ENABLE_GEOIP" in
    auto) [ -r "$GEOIP_DB_PATH" ] && ENABLE_GEOIP=true || ENABLE_GEOIP=false ;;
    true) [ -r "$GEOIP_DB_PATH" ] || die "已要求启用 GeoIP，但无法读取数据库：$GEOIP_DB_PATH" ;;
    false) ;;
    *) die "ENABLE_GEOIP 只能是 auto、true 或 false。" ;;
  esac
  case "$ENABLE_HOST_METRICS" in
    auto) [ -n "$METRICS_URL" ] && ENABLE_HOST_METRICS=true || ENABLE_HOST_METRICS=false ;;
    true) [ -n "$METRICS_URL" ] || die "启用主机指标需要 VictoriaMetrics 写入地址，请设置 METRICS_URL。" ;;
    false) ;;
    *) die "ENABLE_HOST_METRICS 只能是 auto、true 或 false。" ;;
  esac
  command -v docker >/dev/null 2>&1 || die "未找到 Docker。"
  docker info >/dev/null 2>&1 || die "Docker 服务未运行或当前用户无权限访问。"

  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    die "需要 Docker Compose（docker compose 或 docker-compose）。"
  fi

  [ -n "$SERVER_NAME" ] || die "请设置 SERVER_NAME，例如：SERVER_NAME=jp-tokyo-01"
  [[ "$SERVER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die "SERVER_NAME 只能包含字母、数字、点、下划线和连字符。"
  [ -n "$LOKI_URL" ] || die "请设置 LOKI_URL，例如：https://loki.example.com/loki/api/v1/push"
  [[ "$LOKI_URL" =~ ^https://.+/loki/api/v1/push$ ]] || die "LOKI_URL 必须是以 /loki/api/v1/push 结尾的 HTTPS Loki 推送地址。"
  [ -n "$LOKI_USERNAME" ] || die "LOKI_USERNAME 不能为空。"

  METRICS_REQUIRED=false
  if [ "$ENABLE_HOST_METRICS" = true ] || [ -n "$XUI_API_URL" ]; then
    METRICS_REQUIRED=true
  fi
  if [ "$METRICS_REQUIRED" = true ]; then
    [ -n "$METRICS_URL" ] || die "所选指标模块需要 VictoriaMetrics 写入地址，请设置 METRICS_URL。"
    [[ "$METRICS_URL" =~ ^https://.+/api/v1/write$ ]] || die "METRICS_URL 必须是以 /api/v1/write 结尾的 HTTPS 地址。"
    [ -n "$METRICS_USERNAME" ] || die "METRICS_USERNAME 不能为空。"
  fi
  if [ -n "$XUI_API_URL" ]; then
    [[ "$XUI_API_URL" =~ ^https://.+/panel/api/inbounds/list$ ]] || die "XUI_API_URL 必须是 HTTPS 且以 /panel/api/inbounds/list 结尾。"
    case "$XUI_API_URL" in *$'\n'*|*$'\r'*|*\"*) die "XUI_API_URL 包含不支持的字符。" ;; esac
    [ -n "$METRICS_URL" ] || die "启用 3x-ui API 采集时必须同时设置 METRICS_URL。"
  fi
  case "$ENABLE_SECURITY_TRAFFIC" in
    auto)
      if [ "$ENABLE_SECURITY" = true ] && [ "$ENABLE_HOST_METRICS" = true ] && [ -n "$METRICS_URL" ] && command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active$' && command -v iptables >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1; then
        ENABLE_SECURITY_TRAFFIC=true
      else
        ENABLE_SECURITY_TRAFFIC=false
      fi
      ;;
    true)
      [ "$ENABLE_SECURITY" = true ] || die "ENABLE_SECURITY_TRAFFIC=true 需要 ENABLE_SECURITY=true。"
      [ "$ENABLE_HOST_METRICS" = true ] || die "启用 SSH/UFW 聚合流量需要先启用主机指标。"
      [ -n "$METRICS_URL" ] || die "ENABLE_SECURITY_TRAFFIC=true 需要 METRICS_URL。"
      command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active$' || die "ENABLE_SECURITY_TRAFFIC=true 需要已启用的 UFW。"
      command -v iptables >/dev/null 2>&1 || die "ENABLE_SECURITY_TRAFFIC=true 需要 iptables。"
      command -v systemctl >/dev/null 2>&1 || die "ENABLE_SECURITY_TRAFFIC=true 需要 systemd。"
      ;;
    false) ;;
    *) die "ENABLE_SECURITY_TRAFFIC 只能是 auto、true 或 false。" ;;
  esac
  if [ -z "$SSH_PORT" ]; then
    SSH_PORT="$(sshd -T 2>/dev/null | awk '$1 == "port" { print $2; exit }' || true)"
    SSH_PORT="${SSH_PORT:-22}"
  fi
  [[ "$SSH_PORT" =~ ^[0-9]+$ ]] && [ "$SSH_PORT" -ge 1 ] && [ "$SSH_PORT" -le 65535 ] || die "SSH_PORT 必须是 1 到 65535 的端口号。"
  if [ "$ENABLE_SECURITY_TRAFFIC" = true ]; then
    SECURITY_TRAFFIC_VOLUME_LINE="      - $STACK_DIR/textfile:/var/lib/node_exporter/textfile:ro"
    SECURITY_TRAFFIC_EXPORTER_CONFIG='  textfile { directory = "/var/lib/node_exporter/textfile" }'
  else
    SECURITY_TRAFFIC_VOLUME_LINE=""
    SECURITY_TRAFFIC_EXPORTER_CONFIG=""
  fi
}

setup_security_traffic_collector() {
  local service_file=/etc/systemd/system/gla-security-traffic.service
  local timer_file=/etc/systemd/system/gla-security-traffic.timer
  local environment_file="$STACK_DIR/security-traffic.env"
  local collector_file="$STACK_DIR/assets/security_traffic_collector.sh"

  if [ "$ENABLE_SECURITY_TRAFFIC" = false ]; then
    if [ -r "$environment_file" ] && [ -x "$collector_file" ]; then
      set -a
      # shellcheck disable=SC1090
      . "$environment_file"
      set +a
      "$collector_file" --cleanup || true
    fi
    if command -v systemctl >/dev/null 2>&1; then
      systemctl disable --now gla-security-traffic.timer >/dev/null 2>&1 || true
      systemctl daemon-reload
    fi
    rm -f "$service_file" "$timer_file" "$environment_file"
    return
  fi

  install -d -m 0750 "$STACK_DIR/textfile"
  {
    printf 'GLA_TEXTFILE_DIR=%q\n' "$STACK_DIR/textfile"
    printf 'GLA_SERVER_NAME=%q\n' "$SERVER_NAME"
    printf 'GLA_SSH_PORT=%q\n' "$SSH_PORT"
  } >"$environment_file"
  chmod 0600 "$environment_file"

  cat >"$service_file" <<EOF
[Unit]
Description=GLA aggregate SSH and UFW traffic collector
After=network-online.target ufw.service

[Service]
Type=oneshot
EnvironmentFile=$environment_file
ExecStart=$collector_file
EOF
  cat >"$timer_file" <<'EOF'
[Unit]
Description=Run the GLA aggregate firewall traffic collector every 30 seconds

[Timer]
OnBootSec=45s
OnUnitActiveSec=30s
AccuracySec=1s

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now gla-security-traffic.timer
  systemctl start gla-security-traffic.service
}

download_asset() {
  local relative_path="$1" destination="$2"
  if [ -n "${GLA_ASSET_DIR:-}" ] && [ -r "$GLA_ASSET_DIR/$relative_path" ]; then
    install -m 0755 "$GLA_ASSET_DIR/$relative_path" "$destination"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$destination" "$ASSET_BASE_URL/$relative_path"
    chmod 0755 "$destination"
  elif command -v curl >/dev/null 2>&1; then
    curl -fsSL "$ASSET_BASE_URL/$relative_path" -o "$destination"
    chmod 0755 "$destination"
  else
    die "需要 wget 或 curl 下载采集器组件。"
  fi
}

install_geoip_database() {
  local source_path="$1"
  [ -r "$source_path" ] || die "无法读取数据库文件：$source_path"
  install -d -m 0750 "$STACK_DIR/geoip"
  install -m 0640 "$source_path" "$STACK_DIR/geoip/GeoLite2-City.mmdb"
  GEOIP_DB_PATH="$STACK_DIR/geoip/GeoLite2-City.mmdb"
  ENABLE_GEOIP=true
  printf 'GeoIP 数据库已放置并启用：%s\n' "$GEOIP_DB_PATH"
}

download_geoip_database() {
  local tmp_dir db_file http_code size_bytes
  command -v curl >/dev/null 2>&1 || die "从 GitHub 镜像下载需要 curl。请先安装 curl，或选择使用已有文件。"

  tmp_dir="$(mktemp -d)"
  db_file="$tmp_dir/GeoLite2-City.mmdb"
  http_code="$(curl -sSL --retry 2 "$GEOIP_MIRROR_URL" -o "$db_file" -w '%{http_code}')" || { rm -rf "$tmp_dir"; die "无法连接 GeoIP 镜像，请检查网络。"; }
  case "$http_code" in
    200) ;;
    404) rm -rf "$tmp_dir"; die "GeoIP 镜像中未找到 GeoLite2-City.mmdb。" ;;
    429) rm -rf "$tmp_dir"; die "GitHub 请求过于频繁，请稍后再试。" ;;
    *) rm -rf "$tmp_dir"; die "GeoIP 镜像下载失败，HTTP 状态码：$http_code" ;;
  esac
  size_bytes="$(wc -c <"$db_file")"
  [ "$size_bytes" -gt 1048576 ] || { rm -rf "$tmp_dir"; die "下载文件过小，可能不是有效的 GeoLite2-City.mmdb。"; }
  install_geoip_database "$db_file"
  rm -rf "$tmp_dir"
}

prepare_geoip_database() {
  local source_path choice

  if [ "$ENABLE_GEOIP" = false ]; then
    return
  fi
  if [ -r "$GEOIP_DB_PATH" ]; then
    ENABLE_GEOIP=true
    return
  fi

  printf '\n未检测到 GeoIP 数据库。\n'
  printf 'GeoIP 可为来源 IP 添加国家/地区、省份和城市。\n\n'
  printf '1. 从 GitHub GeoLite.mmdb 镜像下载（无需 MaxMind 密钥）\n'
  printf '2. 使用服务器上已有的 GeoLite2-City.mmdb\n'
  printf '0. 跳过 GeoIP\n'
  read -rp "请选择 [0-2]: " choice
  case "$choice" in
    1) download_geoip_database ;;
    2)
      read -rp "已有 GeoLite2-City.mmdb 文件路径: " source_path
      [ -n "$source_path" ] || { [ "$ENABLE_GEOIP" = true ] && die "已要求启用 GeoIP，但没有提供数据库文件。"; ENABLE_GEOIP=false; return; }
      install_geoip_database "$source_path"
      ;;
    0|"")
      [ "$ENABLE_GEOIP" = true ] && die "已要求启用 GeoIP，但没有提供数据库文件。"
      ENABLE_GEOIP=false
      ;;
    *) die "无效选择。" ;;
  esac
}

install_collector() {
  prepare_geoip_database
  require_install_prerequisites
  if [ -z "$LOKI_PASSWORD" ]; then
    read -rsp "请输入 Loki Basic Auth 密码: " LOKI_PASSWORD
    printf '\n'
  fi
  [ -n "$LOKI_PASSWORD" ] || die "LOKI_PASSWORD 不能为空。"
  case "$LOKI_PASSWORD" in
    *$'\n'*|*$'\r'*) die "LOKI_PASSWORD 不能包含换行符。" ;;
  esac

  if [ "$METRICS_REQUIRED" = true ] && [ -z "$METRICS_PASSWORD" ]; then
    read -rsp "请输入指标接口 Basic Auth 密码: " METRICS_PASSWORD
    printf '\n'
  fi
  if [ "$METRICS_REQUIRED" = true ]; then
    [ -n "$METRICS_PASSWORD" ] || die "METRICS_PASSWORD 不能为空。"
    case "$METRICS_PASSWORD" in
      *$'\n'*|*$'\r'*) die "METRICS_PASSWORD 不能包含换行符。" ;;
    esac
  fi

  if [ -n "$XUI_API_URL" ]; then
    if [ -z "$XUI_API_TOKEN" ] && [ -r "$STACK_DIR/secrets/xui-api-token" ]; then
      XUI_API_TOKEN="$(cat "$STACK_DIR/secrets/xui-api-token")"
    fi
    if [ -z "$XUI_API_TOKEN" ]; then
      read -rsp "请输入 3x-ui API Token（输入内容不会显示）: " XUI_API_TOKEN
      printf '\n'
    fi
    [ -n "$XUI_API_TOKEN" ] || die "XUI_API_TOKEN 不能为空。"
    case "$XUI_API_TOKEN" in
      *$'\n'*|*$'\r'*) die "XUI_API_TOKEN 不能包含换行符。" ;;
    esac
  fi

  if [ "$ENABLE_SECURITY" = true ]; then
    if [ -d /var/log/journal ] && [ -n "$(find /var/log/journal -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
      JOURNAL_PATH_HOST=/var/log/journal
    elif [ -d /run/log/journal ]; then
      JOURNAL_PATH_HOST=/run/log/journal
    else
      die "未找到 systemd journal 目录，无法采集 SSH 日志。"
    fi
  fi

  LOKI_URL_HCL="$(printf '%s' "$LOKI_URL" | hcl_escape)"
  LOKI_USERNAME_HCL="$(printf '%s' "$LOKI_USERNAME" | hcl_escape)"
  LOKI_PASSWORD_HCL="$(printf '%s' "$LOKI_PASSWORD" | hcl_escape)"
  METRICS_URL_HCL="$(printf '%s' "$METRICS_URL" | hcl_escape)"
  METRICS_USERNAME_HCL="$(printf '%s' "$METRICS_USERNAME" | hcl_escape)"
  METRICS_PASSWORD_HCL="$(printf '%s' "$METRICS_PASSWORD" | hcl_escape)"

note "正在创建采集器文件：$STACK_DIR"
install -d -m 0750 "$STACK_DIR/alloy" "$STACK_DIR/assets" "$STACK_DIR/secrets" "$STACK_DIR/geoip"
  write_install_settings
  if [ "$ENABLE_SECURITY_TRAFFIC" = true ]; then
    download_asset assets/security_traffic_collector.sh "$STACK_DIR/assets/security_traffic_collector.sh"
    chmod 0750 "$STACK_DIR/assets/security_traffic_collector.sh"
  fi
  setup_security_traffic_collector

  if [ -n "$XUI_API_URL" ]; then
  printf '%s\n' "$XUI_API_TOKEN" >"$STACK_DIR/secrets/xui-api-token"
  chmod 0600 "$STACK_DIR/secrets/xui-api-token"
  download_asset assets/xui_exporter.py "$STACK_DIR/assets/xui_exporter.py"
fi

cat >"$STACK_DIR/compose.yaml" <<EOF
services:
  alloy:
    image: $ALLOY_IMAGE
    container_name: xray-alloy
    restart: unless-stopped
    user: "0:0"
    command: run --storage.path=/var/lib/alloy/data /etc/alloy/config.alloy
    volumes:
      - ./alloy/config.alloy:/etc/alloy/config.alloy:ro
      - alloy-data:/var/lib/alloy/data
EOF

if [ "$ENABLE_HOST_METRICS" = true ]; then
  cat >>"$STACK_DIR/compose.yaml" <<'EOF'
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/host/root:ro,rslave
EOF
fi

if [ "$ENABLE_XRAY" = true ]; then
  printf '      - %s:%s:ro\n' "$XRAY_LOG" "$XRAY_LOG" >>"$STACK_DIR/compose.yaml"
fi

if [ "$ENABLE_GEOIP" = true ]; then
  printf '      - %s:/var/lib/gla/geoip/GeoLite2-City.mmdb:ro\n' "$GEOIP_DB_PATH" >>"$STACK_DIR/compose.yaml"
fi

if [ "$ENABLE_SECURITY" = true ]; then
  cat >>"$STACK_DIR/compose.yaml" <<EOF
      - $JOURNAL_PATH_HOST:/var/log/journal:ro
      - /etc/machine-id:/etc/machine-id:ro
      - /var/log:/host/var/log:ro
EOF
fi

if [ "$ENABLE_SECURITY_TRAFFIC" = true ]; then
  printf '      - %s/textfile:/var/lib/node_exporter/textfile:ro\n' "$STACK_DIR" >>"$STACK_DIR/compose.yaml"
fi

if [ -n "$XUI_API_URL" ]; then
  cat >>"$STACK_DIR/compose.yaml" <<EOF

  xui-exporter:
    image: python:3.12-alpine
    container_name: gla-xui-exporter
    restart: unless-stopped
    command: ["python3", "/app/xui_exporter.py"]
    environment:
      XUI_API_URL: "$XUI_API_URL"
      XUI_API_TOKEN_FILE: /run/secrets/xui_api_token
      SERVER_NAME: "$SERVER_NAME"
    volumes:
      - ./assets/xui_exporter.py:/app/xui_exporter.py:ro
      - ./secrets/xui-api-token:/run/secrets/xui_api_token:ro
EOF
fi

cat >>"$STACK_DIR/compose.yaml" <<'EOF'

volumes:
  alloy-data:
EOF

cat >"$STACK_DIR/alloy/config.alloy" <<EOF
loki.write "central" {
  endpoint {
    url = "$LOKI_URL_HCL"

    basic_auth {
      username = "$LOKI_USERNAME_HCL"
      password = "$LOKI_PASSWORD_HCL"
    }
  }
}
EOF

if [ "$ENABLE_XRAY" = true ]; then
  cat >>"$STACK_DIR/alloy/config.alloy" <<EOF

local.file_match "xray_access" {
  path_targets = [{
    __path__ = "$XRAY_LOG",
    job      = "xray-access",
    server   = "$SERVER_NAME",
  }]
}

loki.source.file "xray_access" {
  targets    = local.file_match.xray_access.targets
  forward_to = [loki.process.xray_access.receiver]
}

loki.process "xray_access" {
  forward_to = [loki.write.central.receiver]

  stage.regex {
    expression = "^(?P<timestamp>\\\\d{4}/\\\\d{2}/\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2}\\\\.\\\\d+) from (?P<source_ip>(?:\\\\[[0-9A-Fa-f:]+\\\\]|[0-9A-Fa-f:.]+)):[0-9]+ accepted (?P<network>\\\\w+):(?P<destination>\\\\S+) \\\\[(?P<inbound>[^ ]+) (?:>>|->) (?P<outbound>[^\\\\]]+)\\\\](?: email: (?P<email>\\\\S+))?"
  }

EOF
  if [ "$ENABLE_GEOIP" = true ]; then
    cat >>"$STACK_DIR/alloy/config.alloy" <<'EOF'

  stage.geoip {
    db      = "/var/lib/gla/geoip/GeoLite2-City.mmdb"
    source  = "source_ip"
    db_type = "city"
    custom_lookups = {
      geo_country_localized = "country.names.\"zh-CN\" || country.names.en",
      geo_region_localized  = "subdivisions[0].names.\"zh-CN\" || subdivisions[0].names.en",
      geo_city_localized    = "city.names.\"zh-CN\" || city.names.en",
    }
  }

  stage.labels {
    values = {
      geo_country = "geo_country_localized",
      geo_region  = "geo_region_localized",
      geo_city    = "geo_city_localized",
    }
  }
EOF
  fi

  cat >>"$STACK_DIR/alloy/config.alloy" <<'EOF'

  stage.timestamp {
    source = "timestamp"
    format = "2006/01/02 15:04:05.000000"
  }

  stage.labels {
    values = {
      inbound  = "",
      outbound = "",
      email    = "",
    }
  }
}
EOF
fi

if [ "$ENABLE_SECURITY" = true ]; then
  cat >>"$STACK_DIR/alloy/config.alloy" <<EOF

loki.source.journal "ssh" {
  path       = "/var/log/journal"
  matches    = "_SYSTEMD_UNIT=ssh.service"
  labels     = { job = "ssh-journal", server = "$SERVER_NAME" }
  forward_to = [loki.process.ssh.receiver]
}

loki.source.journal "sshd" {
  path       = "/var/log/journal"
  matches    = "_SYSTEMD_UNIT=sshd.service"
  labels     = { job = "ssh-journal", server = "$SERVER_NAME" }
  forward_to = [loki.process.ssh.receiver]
}

loki.process "ssh" {
  forward_to = [loki.write.central.receiver]

  stage.regex {
    expression = "from (?P<source_ip>(?:\\\\[[0-9A-Fa-f:]+\\\\]|[0-9A-Fa-f:.]+))"
  }

EOF
  if [ "$ENABLE_GEOIP" = true ]; then
    cat >>"$STACK_DIR/alloy/config.alloy" <<'EOF'
  stage.geoip {
    db      = "/var/lib/gla/geoip/GeoLite2-City.mmdb"
    source  = "source_ip"
    db_type = "city"
    custom_lookups = {
      geo_country_localized = "country.names.\"zh-CN\" || country.names.en",
      geo_region_localized  = "subdivisions[0].names.\"zh-CN\" || subdivisions[0].names.en",
      geo_city_localized    = "city.names.\"zh-CN\" || city.names.en",
    }
  }

  stage.labels {
    values = {
      geo_country = "geo_country_localized",
      geo_region  = "geo_region_localized",
      geo_city    = "geo_city_localized",
    }
  }
EOF
  fi
  cat >>"$STACK_DIR/alloy/config.alloy" <<EOF
}

local.file_match "fail2ban" {
  path_targets = [{
    __path__ = "/host/var/log/fail2ban.log",
    job      = "fail2ban",
    server   = "$SERVER_NAME",
  }]
}

loki.source.file "fail2ban" {
  targets    = local.file_match.fail2ban.targets
  forward_to = [loki.process.fail2ban.receiver]
}

loki.process "fail2ban" {
  forward_to = [loki.write.central.receiver]

  stage.regex {
    expression = "^(?P<timestamp>\\\\d{4}-\\\\d{2}-\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2},\\\\d+).*?(?:Found|Ban|Unban) (?P<source_ip>(?:\\\\[[0-9A-Fa-f:]+\\\\]|[0-9A-Fa-f:.]+))"
  }

EOF
  if [ "$ENABLE_GEOIP" = true ]; then
    cat >>"$STACK_DIR/alloy/config.alloy" <<'EOF'
  stage.geoip {
    db      = "/var/lib/gla/geoip/GeoLite2-City.mmdb"
    source  = "source_ip"
    db_type = "city"
    custom_lookups = {
      geo_country_localized = "country.names.\"zh-CN\" || country.names.en",
      geo_region_localized  = "subdivisions[0].names.\"zh-CN\" || subdivisions[0].names.en",
      geo_city_localized    = "city.names.\"zh-CN\" || city.names.en",
    }
  }

  stage.labels {
    values = {
      geo_country = "geo_country_localized",
      geo_region  = "geo_region_localized",
      geo_city    = "geo_city_localized",
    }
  }

EOF
  fi
  cat >>"$STACK_DIR/alloy/config.alloy" <<EOF

  stage.timestamp {
    source = "timestamp"
    format = "2006-01-02 15:04:05,000"
  }
}

local.file_match "ufw" {
  path_targets = [{
    __path__ = "/host/var/log/ufw.log",
    job      = "ufw",
    server   = "$SERVER_NAME",
  }]
}

loki.source.file "ufw" {
  targets    = local.file_match.ufw.targets
  forward_to = [loki.write.central.receiver]
}
EOF
fi

if [ "$ENABLE_HOST_METRICS" = true ]; then
  cat >>"$STACK_DIR/alloy/config.alloy" <<EOF

prometheus.exporter.unix "host" {
  procfs_path = "/host/proc"
  sysfs_path  = "/host/sys"
  rootfs_path = "/host/root"
${SECURITY_TRAFFIC_EXPORTER_CONFIG}
}

discovery.relabel "host" {
  targets = prometheus.exporter.unix.host.targets

  rule {
    target_label = "server"
    replacement  = "$SERVER_NAME"
  }
}

prometheus.scrape "host" {
  targets         = discovery.relabel.host.output
  scrape_interval = "30s"
  forward_to      = [prometheus.remote_write.central.receiver]
}
EOF

fi

if [ -n "$XUI_API_URL" ]; then
  cat >>"$STACK_DIR/alloy/config.alloy" <<EOF

prometheus.scrape "xui" {
  targets = [{
    __address__ = "xui-exporter:9105",
    job         = "xui-metrics",
    server      = "$SERVER_NAME",
  }]
  scrape_interval = "30s"
  honor_labels    = true
  forward_to      = [prometheus.remote_write.central.receiver]
}
EOF
fi

if [ "$METRICS_REQUIRED" = true ]; then
  cat >>"$STACK_DIR/alloy/config.alloy" <<EOF

prometheus.remote_write "central" {
  endpoint {
    url = "$METRICS_URL_HCL"

    basic_auth {
      username = "$METRICS_USERNAME_HCL"
      password = "$METRICS_PASSWORD_HCL"
    }
  }
}
EOF
fi

chmod 0600 "$STACK_DIR/compose.yaml" "$STACK_DIR/alloy/config.alloy" "$INSTALL_SETTINGS_FILE"

note "正在启动 Alloy 采集器"
cd "$STACK_DIR"
compose pull
compose up -d
if [ -n "$XUI_API_URL" ]; then
  compose restart xui-exporter
else
  docker rm -f gla-xui-exporter >/dev/null 2>&1 || true
fi
compose restart alloy

note "正在验证采集器状态"
compose ps

install_manager

cat <<EOF

模块化采集器部署完成。

服务器标签：$SERVER_NAME
中心 Loki：$LOKI_URL
Xray 日志：$ENABLE_XRAY
安全日志：$ENABLE_SECURITY
主机指标：$([ "$ENABLE_HOST_METRICS" = true ] && printf '已启用' || printf '未启用')
3x-ui API：$([ -n "$XUI_API_URL" ] && printf '已启用' || printf '未启用')

管理命令：
  alloy

远程服务器只需要允许出站 HTTPS 访问，无需开放新的入站端口。
EOF
}

install_manager() {
  if [ -e "$MANAGER_PATH" ] && ! grep -Eq "Xray Alloy Collector Manager|GLA Alloy Collector Manager" "$MANAGER_PATH" 2>/dev/null; then
    die "$MANAGER_PATH 已存在且不是本采集器管理脚本。请使用其他命令名称。"
  fi

  cat >"$MANAGER_PATH" <<'EOF'
#!/usr/bin/env bash
# GLA Alloy Collector Manager
set -Eeuo pipefail

GLA_VERSION="2.2.2"
STACK_DIR="${STACK_DIR:-/opt/xray-alloy-collector}"
COMPOSE_FILE="$STACK_DIR/compose.yaml"
INSTALL_SETTINGS_FILE="$STACK_DIR/.install.env"
INSTALLER_URL="https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-alloy-collector.sh"

die() { printf '错误: %s\n' "$*" >&2; exit 1; }
pause() { read -rp "按 Enter 键继续..." _; }
confirm() {
  local prompt="$1"
  read -rp "$prompt [Y/N]: " answer
  [[ "$answer" =~ ^[Yy]$ ]]
}

container_state() {
  local container="$1" state
  state="$(docker inspect -f '{{.State.Status}}' "$container" 2>/dev/null || true)"
  case "$state" in
    running) printf '[运行中]' ;;
    restarting) printf '[重启中]' ;;
    exited|dead) printf '[已停止]' ;;
    created|paused) printf '[%s]' "$state" ;;
    *) printf '[未安装]' ;;
  esac
}

boolean_state() {
  if [ "$1" = true ]; then
    printf '[已启用]'
  else
    printf '[未启用]'
  fi
}

xui_state() {
  if [ -z "$1" ]; then
    printf '[未启用]'
  else
    container_state gla-xui-exporter
  fi
}

show_header() {
  local server_name="未知服务器" enable_xray="" enable_security="" enable_geoip=""
  local enable_host_metrics="" enable_security_traffic="" metrics_url="" xui_api_url=""
  local STACK_DIR XRAY_LOG ENABLE_XRAY ENABLE_SECURITY ENABLE_GEOIP ENABLE_HOST_METRICS
  local ENABLE_SECURITY_TRAFFIC SSH_PORT GEOIP_DB_PATH GEOIP_MIRROR_URL SERVER_NAME
  local LOKI_URL LOKI_USERNAME METRICS_URL METRICS_USERNAME XUI_API_URL ALLOY_IMAGE
  if [ -r "$INSTALL_SETTINGS_FILE" ]; then
    set +u
    # Generated locally by GLA and restricted to root.
    . "$INSTALL_SETTINGS_FILE"
    set -u
    server_name="${SERVER_NAME:-未知服务器}"
    enable_xray="${ENABLE_XRAY:-}"
    enable_security="${ENABLE_SECURITY:-}"
    enable_geoip="${ENABLE_GEOIP:-}"
    metrics_url="${METRICS_URL:-}"
    xui_api_url="${XUI_API_URL:-}"
    enable_security_traffic="${ENABLE_SECURITY_TRAFFIC:-false}"
    if grep -q '^ENABLE_HOST_METRICS=' "$INSTALL_SETTINGS_FILE"; then
      enable_host_metrics="$ENABLE_HOST_METRICS"
    elif [ -n "$metrics_url" ]; then
      # 2.1.x and earlier inferred host metrics from METRICS_URL.
      enable_host_metrics=true
    else
      enable_host_metrics=false
    fi
  fi

  printf 'GLA Alloy %s - %s\n\n' "$GLA_VERSION" "$server_name"
  printf 'Alloy          %s    Xray 日志      %s\n' "$(container_state xray-alloy)" "$(boolean_state "$enable_xray")"
  printf '安全日志       %s    主机指标       %s\n' "$(boolean_state "$enable_security")" "$(boolean_state "$enable_host_metrics")"
  printf 'GeoIP 归属解析  %s    SSH/UFW 流量   %s\n' "$(boolean_state "$enable_geoip")" "$(boolean_state "$enable_security_traffic")"
  printf '3x-ui 流量采集 %s\n' "$(xui_state "$xui_api_url")"
}

configured_value() {
  if [ -n "$1" ]; then
    printf '%s' "$1"
  else
    printf '未配置'
  fi
}

secret_state() {
  if [ -f "$1" ] && [ -s "$1" ]; then
    printf '已配置（内容已隐藏）'
  else
    printf '未配置'
  fi
}

current_journal_path() {
  if [ -d /var/log/journal ] && [ -n "$(find /var/log/journal -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    printf '/var/log/journal'
  elif [ -d /run/log/journal ]; then
    printf '/run/log/journal'
  else
    printf '未检测到'
  fi
}

print_setting() {
  printf '  %s %s\n' "$1：" "$2"
}

compose() {
  [ -f "$COMPOSE_FILE" ] || die "采集器未安装：$STACK_DIR"
  if docker compose version >/dev/null 2>&1; then
    docker compose -f "$COMPOSE_FILE" "$@"
  elif command -v docker-compose >/dev/null 2>&1; then
    docker-compose -f "$COMPOSE_FILE" "$@"
  else
    die "需要 Docker Compose。"
  fi
}

show_status() {
  if [ -f "$COMPOSE_FILE" ]; then
    compose ps
  else
    printf '未找到采集器配置。可能仍保留 Docker 数据卷。\n'
  fi

  local alloy_image volume mountpoint log_path
  alloy_image="$(docker inspect -f '{{.Config.Image}}' xray-alloy 2>/dev/null || printf '%s' 'grafana/alloy:latest')"
  printf '\nAlloy 镜像占用：\n'
  docker image ls "$alloy_image"

  printf '\nAlloy 数据卷占用：\n'
  while IFS= read -r volume; do
    [ -n "$volume" ] || continue
    mountpoint="$(docker volume inspect -f '{{.Mountpoint}}' "$volume")"
    du -sh "$mountpoint"
  done < <(docker volume ls -q --filter label=com.docker.compose.project=xray-alloy-collector)

  log_path="$(sed -n 's/^[[:space:]]*__path__[[:space:]]*=[[:space:]]*"\(.*\)",/\1/p' "$STACK_DIR/alloy/config.alloy" 2>/dev/null | head -n 1)"
  if [ -n "$log_path" ] && [ -e "$log_path" ]; then
    printf '\nXray 原始访问日志占用：\n'
    du -sh "$log_path"
  fi
}

show_logs() {
  printf '\n采集器日志\n\n1. Alloy\n2. 3x-ui API 流量采集\n0. 返回\n'
  read -rp "请选择服务: " choice
  case "$choice" in
    1) compose logs -f --tail=100 alloy || true ;;
    2)
      if grep -q '^  xui-exporter:' "$COMPOSE_FILE" 2>/dev/null; then
        compose logs -f --tail=100 xui-exporter || true
      else
        printf '3x-ui API 流量采集未启用。\n'
      fi
      ;;
    0) return ;;
    *) printf '无效选择。\n' ;;
  esac
}

show_config() {
  local metrics_write_active=false
  load_module_settings
  grep -q '^prometheus.remote_write "central"' "$STACK_DIR/alloy/config.alloy" 2>/dev/null && metrics_write_active=true
  printf '\n采集器当前设置\n\n'
  print_setting "服务器标识" "${SERVER_NAME:-未配置}"
  print_setting "安装目录" "$STACK_DIR"
  print_setting "Alloy 镜像" "${ALLOY_IMAGE:-grafana/alloy:latest}"

  printf '\n日志写入\n'
  print_setting "Loki 地址" "$(configured_value "${LOKI_URL:-}")"
  print_setting "认证用户名" "$(configured_value "${LOKI_USERNAME:-}")"
  print_setting "认证密码" "已配置（内容已隐藏）"

  printf '\n指标写入\n'
  print_setting "VictoriaMetrics 地址" "$(configured_value "${METRICS_URL:-}")"
  print_setting "认证用户名" "$(configured_value "${METRICS_USERNAME:-}")"
  if [ "$metrics_write_active" = true ]; then
    print_setting "认证密码" "已配置（内容已隐藏）"
  else
    print_setting "认证密码" "当前未使用"
  fi

  printf '\n采集模块\n'
  print_setting "Xray 日志" "$(boolean_state "$ENABLE_XRAY")，路径：$(configured_value "${XRAY_LOG:-}")"
  print_setting "安全日志" "$(boolean_state "$ENABLE_SECURITY")，Journal：$(current_journal_path)"
  print_setting "主机指标" "$(boolean_state "$ENABLE_HOST_METRICS")，采集间隔：30 秒"
  print_setting "GeoIP 归属解析" "$(boolean_state "$ENABLE_GEOIP")，数据库：$(configured_value "${GEOIP_DB_PATH:-}")"
  print_setting "SSH/UFW 聚合流量" "$(boolean_state "$ENABLE_SECURITY_TRAFFIC")，SSH 端口：${SSH_PORT:-自动检测}，采集间隔：30 秒"
  print_setting "3x-ui API 流量" "$([ -n "${XUI_API_URL:-}" ] && printf '[已启用]' || printf '[未启用]')，API：$(configured_value "${XUI_API_URL:-}")"
  print_setting "3x-ui API Token（已保存）" "$(secret_state "$STACK_DIR/secrets/xui-api-token")"

  printf '\n说明：密码和 Token 不会回显；“已配置”表示部署配置中已使用对应凭据。\n'
}

download_installer() {
  if command -v wget >/dev/null 2>&1; then
    wget -qO- "$INSTALLER_URL"
  elif command -v curl >/dev/null 2>&1; then
    curl -fsSL "$INSTALLER_URL"
  else
    die "需要 wget 或 curl 才能更新脚本。"
  fi
}

update_script_and_deploy() {
  local xui_api_url_is_set="${XUI_API_URL+x}" xui_api_url_override="${XUI_API_URL-}"
  local metrics_url_is_set="${METRICS_URL+x}" metrics_url_override="${METRICS_URL-}"
  local metrics_username_is_set="${METRICS_USERNAME+x}" metrics_username_override="${METRICS_USERNAME-}"
  local enable_xray_is_set="${ENABLE_XRAY+x}" enable_xray_override="${ENABLE_XRAY-}"
  local xray_log_is_set="${XRAY_LOG+x}" xray_log_override="${XRAY_LOG-}"
  local enable_security_is_set="${ENABLE_SECURITY+x}" enable_security_override="${ENABLE_SECURITY-}"
  local enable_geoip_is_set="${ENABLE_GEOIP+x}" enable_geoip_override="${ENABLE_GEOIP-}"
  local geoip_db_path_is_set="${GEOIP_DB_PATH+x}" geoip_db_path_override="${GEOIP_DB_PATH-}"
  local enable_host_metrics_is_set="${ENABLE_HOST_METRICS+x}" enable_host_metrics_override="${ENABLE_HOST_METRICS-}"
  local enable_security_traffic_is_set="${ENABLE_SECURITY_TRAFFIC+x}" enable_security_traffic_override="${ENABLE_SECURITY_TRAFFIC-}"
  [ -r "$INSTALL_SETTINGS_FILE" ] || die "未找到安装配置：$INSTALL_SETTINGS_FILE"
  printf '正在从 GitHub 下载最新版脚本并重新部署。请输入现有 Loki 密码以保留连接配置。\n'
  set -a
  # This file is created locally by the installer and intentionally excludes the password.
  . "$INSTALL_SETTINGS_FILE"
  set +a
  if [ "$xui_api_url_is_set" = x ]; then
    XUI_API_URL="$xui_api_url_override"
    export XUI_API_URL
  fi
  if [ "$metrics_url_is_set" = x ]; then
    METRICS_URL="$metrics_url_override"
    export METRICS_URL
  fi
  if [ "$metrics_username_is_set" = x ]; then
    METRICS_USERNAME="$metrics_username_override"
    export METRICS_USERNAME
  fi
  if [ "$enable_xray_is_set" = x ]; then
    ENABLE_XRAY="$enable_xray_override"
    export ENABLE_XRAY
  fi
  if [ "$xray_log_is_set" = x ]; then
    XRAY_LOG="$xray_log_override"
    export XRAY_LOG
  fi
  if [ "$enable_security_is_set" = x ]; then
    ENABLE_SECURITY="$enable_security_override"
    export ENABLE_SECURITY
  fi
  if [ "$enable_geoip_is_set" = x ]; then
    ENABLE_GEOIP="$enable_geoip_override"
    export ENABLE_GEOIP
  elif grep -q '^ENABLE_GEOIP=' "$INSTALL_SETTINGS_FILE"; then
    export ENABLE_GEOIP
  else
    ENABLE_GEOIP=auto
    export ENABLE_GEOIP
  fi
  if [ "$geoip_db_path_is_set" = x ]; then
    GEOIP_DB_PATH="$geoip_db_path_override"
    export GEOIP_DB_PATH
  fi
  if [ "$enable_host_metrics_is_set" = x ]; then
    ENABLE_HOST_METRICS="$enable_host_metrics_override"
  elif grep -q '^ENABLE_HOST_METRICS=' "$INSTALL_SETTINGS_FILE"; then
    : # Keep the explicit value loaded from the current settings file.
  elif [ -n "${METRICS_URL:-}" ]; then
    # Preserve the behavior of settings written before ENABLE_HOST_METRICS existed.
    ENABLE_HOST_METRICS=true
  else
    ENABLE_HOST_METRICS=false
  fi
  export ENABLE_HOST_METRICS
  if [ "$enable_security_traffic_is_set" = x ]; then
    ENABLE_SECURITY_TRAFFIC="$enable_security_traffic_override"
    export ENABLE_SECURITY_TRAFFIC
  fi
  ALLOY_ACTION=install bash <(download_installer)
}

load_module_settings() {
  [ -r "$INSTALL_SETTINGS_FILE" ] || die "未找到安装配置：$INSTALL_SETTINGS_FILE"
  set +u
  . "$INSTALL_SETTINGS_FILE"
  set -u
  ENABLE_XRAY="${ENABLE_XRAY:-false}"
  ENABLE_SECURITY="${ENABLE_SECURITY:-false}"
  ENABLE_GEOIP="${ENABLE_GEOIP:-false}"
  ENABLE_SECURITY_TRAFFIC="${ENABLE_SECURITY_TRAFFIC:-false}"
  if ! grep -q '^ENABLE_HOST_METRICS=' "$INSTALL_SETTINGS_FILE"; then
    [ -n "${METRICS_URL:-}" ] && ENABLE_HOST_METRICS=true || ENABLE_HOST_METRICS=false
  fi
}

configure_metrics_endpoint() {
  local answer current_url="${METRICS_URL:-}" current_username="${METRICS_USERNAME:-alloy-agent}"
  printf '\n该模块需要 VictoriaMetrics Remote Write 服务。\n'
  printf '地址格式：https://指标域名/api/v1/write\n'
  if [ -n "$current_url" ]; then
    printf '当前地址：%s\n' "$current_url"
    read -rp "新地址（直接按 Enter 保留）: " answer
    [ -n "$answer" ] && METRICS_URL="$answer"
  else
    read -rp "指标写入地址: " METRICS_URL
    [ -n "$METRICS_URL" ] || { printf '未配置指标写入地址，无法启用。\n'; return 1; }
  fi
  [[ "$METRICS_URL" =~ ^https://.+/api/v1/write$ ]] || { printf '地址无效：必须使用 HTTPS 并以 /api/v1/write 结尾。\n'; return 1; }
  read -rp "指标认证用户名 [${current_username}]: " answer
  METRICS_USERNAME="${answer:-$current_username}"
  export METRICS_URL METRICS_USERNAME
  printf '重新部署时将安全询问指标接口密码。\n'
}

show_xray_current_config() {
  print_setting "访问日志路径" "$(configured_value "${XRAY_LOG:-}")"
  print_setting "写入 Loki" "$(configured_value "${LOKI_URL:-}")"
  print_setting "Loki 用户名" "$(configured_value "${LOKI_USERNAME:-}")"
  print_setting "GeoIP 增强" "$(boolean_state "$ENABLE_GEOIP")"
}

show_security_current_config() {
  print_setting "SSH Journal" "$(current_journal_path)"
  print_setting "Fail2ban 日志" "/var/log/fail2ban.log"
  print_setting "UFW 日志" "/var/log/ufw.log"
  print_setting "写入 Loki" "$(configured_value "${LOKI_URL:-}")"
  print_setting "GeoIP 增强" "$(boolean_state "$ENABLE_GEOIP")"
}

show_host_metrics_current_config() {
  print_setting "写入地址" "$(configured_value "${METRICS_URL:-}")"
  print_setting "认证用户名" "$(configured_value "${METRICS_USERNAME:-}")"
  print_setting "采集间隔" "30 秒"
}

show_geoip_current_config() {
  local database_state="未检测到"
  [ -r "${GEOIP_DB_PATH:-}" ] && database_state="可读取"
  print_setting "数据库路径" "$(configured_value "${GEOIP_DB_PATH:-}")"
  print_setting "数据库状态" "$database_state"
  print_setting "增强 Xray 日志" "$(boolean_state "$ENABLE_XRAY")"
  print_setting "增强安全日志" "$(boolean_state "$ENABLE_SECURITY")"
}

show_security_traffic_current_config() {
  local ufw_state="未安装"
  if command -v ufw >/dev/null 2>&1; then
    ufw status 2>/dev/null | grep -q '^Status: active$' && ufw_state="已启用" || ufw_state="未启用"
  fi
  print_setting "SSH 端口" "${SSH_PORT:-自动检测}"
  print_setting "UFW 状态" "$ufw_state"
  print_setting "主机指标依赖" "$(boolean_state "$ENABLE_HOST_METRICS")"
  print_setting "安全日志依赖" "$(boolean_state "$ENABLE_SECURITY")"
  print_setting "写入地址" "$(configured_value "${METRICS_URL:-}")"
  print_setting "采集间隔" "30 秒"
}

show_xui_current_config() {
  print_setting "Panel API 地址" "$(configured_value "${XUI_API_URL:-}")"
  print_setting "API Token" "$(secret_state "$STACK_DIR/secrets/xui-api-token")"
  print_setting "写入地址" "$(configured_value "${METRICS_URL:-}")"
  print_setting "认证用户名" "$(configured_value "${METRICS_USERNAME:-}")"
  print_setting "采集间隔" "30 秒"
}

read_module_action() {
  local title="$1" current="$2" details_function="${3:-}"
  printf '\n%s\n当前状态：%s\n' "$title" "$(boolean_state "$current")"
  if [ -n "$details_function" ]; then
    printf '\n当前使用配置\n'
    "$details_function"
  fi
  printf '\n1. 启用或更新配置\n2. 关闭\n0. 返回\n'
  read -rp "请选择 [0-2]: " MODULE_ACTION
  case "$MODULE_ACTION" in
    0|1|2) ;;
    *) printf '无效选择。\n'; return 1 ;;
  esac
}

configure_host_metrics() {
  local current
  load_module_settings
  current="$ENABLE_HOST_METRICS"
  read_module_action "主机指标配置" "$current" show_host_metrics_current_config || return 1
  case "$MODULE_ACTION" in
    0) return 1 ;;
    1)
      configure_metrics_endpoint || return 1
      ENABLE_HOST_METRICS=true
      ;;
    2)
      ENABLE_HOST_METRICS=false
      if [ "$ENABLE_SECURITY_TRAFFIC" != false ]; then
        ENABLE_SECURITY_TRAFFIC=false
        printf '提醒：SSH/UFW 聚合流量依赖主机指标，已同时关闭。\n'
      fi
      if [ -n "${XUI_API_URL:-}" ]; then
        printf '提醒：3x-ui 流量仍使用当前 VictoriaMetrics 地址，地址将被保留。\n'
      fi
      ;;
  esac
  export ENABLE_HOST_METRICS ENABLE_SECURITY_TRAFFIC
  update_script_and_deploy
}

configure_xray_logs() {
  local answer current
  load_module_settings
  current="$ENABLE_XRAY"
  read_module_action "Xray 日志配置" "$current" show_xray_current_config || return 1
  case "$MODULE_ACTION" in
    0) return 1 ;;
    1)
      printf 'Xray 日志需要可读取的 access.log，并通过 Loki 写入。\n'
      if [ -n "${XRAY_LOG:-}" ]; then
        printf '当前路径：%s\n' "$XRAY_LOG"
        read -rp "新路径（直接按 Enter 保留）: " answer
        [ -n "$answer" ] && XRAY_LOG="$answer"
      else
        read -rp "Xray access.log 路径: " XRAY_LOG
      fi
      [ -r "$XRAY_LOG" ] || { printf '无法读取 Xray 日志：%s\n请确认 3x-ui/Xray 已启用访问日志及路径权限。\n' "$XRAY_LOG"; return 1; }
      ENABLE_XRAY=true
      ;;
    2) ENABLE_XRAY=false ;;
  esac
  export ENABLE_XRAY XRAY_LOG
  update_script_and_deploy
}

configure_security_logs() {
  local current
  load_module_settings
  current="$ENABLE_SECURITY"
  read_module_action "安全日志配置（SSH、Fail2ban、UFW）" "$current" show_security_current_config || return 1
  case "$MODULE_ACTION" in
    0) return 1 ;;
    1)
      printf '安全日志需要 systemd journal；Fail2ban/UFW 未安装时对应日志为空。\n'
      if ! { [ -d /var/log/journal ] && [ -n "$(find /var/log/journal -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; } && [ ! -d /run/log/journal ]; then
        printf '未找到 systemd journal，无法启用安全日志。\n'
        return 1
      fi
      ENABLE_SECURITY=true
      ;;
    2)
      ENABLE_SECURITY=false
      if [ "$ENABLE_SECURITY_TRAFFIC" != false ]; then
        ENABLE_SECURITY_TRAFFIC=false
        printf '提醒：SSH/UFW 聚合流量依赖安全日志，已同时关闭。\n'
      fi
      ;;
  esac
  export ENABLE_SECURITY ENABLE_SECURITY_TRAFFIC
  update_script_and_deploy
}

configure_geoip() {
  local current
  load_module_settings
  current="$ENABLE_GEOIP"
  read_module_action "GeoIP 归属解析配置" "$current" show_geoip_current_config || return 1
  case "$MODULE_ACTION" in
    0) return 1 ;;
    1)
      if [ "$ENABLE_XRAY" != true ] && [ "$ENABLE_SECURITY" != true ]; then
        printf '提醒：GeoIP 只增强 Xray 或安全日志；当前两个日志模块都未启用。\n'
      fi
      ENABLE_GEOIP=true
      printf '如果本机没有 GeoLite2-City.mmdb，重新部署时会提示下载或选择文件。\n'
      ;;
    2) ENABLE_GEOIP=false ;;
  esac
  export ENABLE_GEOIP
  update_script_and_deploy
}

configure_security_traffic() {
  local current
  load_module_settings
  current="$ENABLE_SECURITY_TRAFFIC"
  read_module_action "SSH/UFW 聚合流量配置" "$current" show_security_traffic_current_config || return 1
  case "$MODULE_ACTION" in
    0) return 1 ;;
    1)
      [ "$ENABLE_HOST_METRICS" = true ] || { printf '无法启用：请先启用主机指标和 VictoriaMetrics 写入。\n'; return 1; }
      [ "$ENABLE_SECURITY" = true ] || { printf '无法启用：请先启用安全日志。\n'; return 1; }
      command -v systemctl >/dev/null 2>&1 || { printf '无法启用：系统需要 systemd。\n'; return 1; }
      command -v iptables >/dev/null 2>&1 || { printf '无法启用：系统需要 iptables。\n'; return 1; }
      command -v ufw >/dev/null 2>&1 || { printf '无法启用：系统需要安装 UFW。\n'; return 1; }
      ufw status 2>/dev/null | grep -q '^Status: active$' || { printf '无法启用：请先启用 UFW。\n'; return 1; }
      ENABLE_SECURITY_TRAFFIC=true
      ;;
    2) ENABLE_SECURITY_TRAFFIC=false ;;
  esac
  export ENABLE_SECURITY_TRAFFIC
  update_script_and_deploy
}

configure_xui_api() {
  local current_url current_enabled=false answer
  load_module_settings
  current_url="${XUI_API_URL:-}"
  [ -n "$current_url" ] && current_enabled=true
  read_module_action "3x-ui API 流量采集配置" "$current_enabled" show_xui_current_config || return 1
  case "$MODULE_ACTION" in
    0) return 1 ;;
    1)
      printf '3x-ui 流量需要 Panel API、API Token 和 VictoriaMetrics 写入服务。\n'
      printf '格式：https://面板域名/面板路径/panel/api/inbounds/list\n'
      if [ -n "$current_url" ]; then
        read -rp "新 API 地址（直接按 Enter 保留）: " answer
        [ -n "$answer" ] && XUI_API_URL="$answer"
      else
        read -rp "3x-ui API 地址: " XUI_API_URL
        [ -n "$XUI_API_URL" ] || { printf '未配置 API 地址，无法启用。\n'; return 1; }
      fi
      [[ "$XUI_API_URL" =~ ^https://.+/panel/api/inbounds/list$ ]] || { printf '地址无效：必须使用 HTTPS 并以 /panel/api/inbounds/list 结尾。\n'; return 1; }
      configure_metrics_endpoint || return 1
      ;;
    2) XUI_API_URL="" ;;
    *) printf '无效选择。\n'; return 1 ;;
  esac
  export XUI_API_URL
  update_script_and_deploy
}

configure_modules() {
  local choice enable_host_metrics
  while true; do
    load_module_settings
    enable_host_metrics="$ENABLE_HOST_METRICS"
    printf '\n采集模块配置\n\n'
    printf '1. Xray 日志                   %s\n' "$(boolean_state "$ENABLE_XRAY")"
    printf '2. 安全日志（SSH/Fail2ban/UFW）%s\n' "$(boolean_state "$ENABLE_SECURITY")"
    printf '3. 主机指标                    %s\n' "$(boolean_state "$enable_host_metrics")"
    printf '4. GeoIP 归属解析              %s\n' "$(boolean_state "$ENABLE_GEOIP")"
    printf '5. SSH/UFW 聚合流量            %s\n' "$(boolean_state "$ENABLE_SECURITY_TRAFFIC")"
    printf '6. 3x-ui API 流量              %s\n' "$([ -n "${XUI_API_URL:-}" ] && printf '[已启用]' || printf '[未启用]')"
    printf '0. 返回\n'
    read -rp "请选择模块 [0-6]: " choice
    case "$choice" in
      0) return 1 ;;
      1) configure_xray_logs && return 0 ;;
      2) configure_security_logs && return 0 ;;
      3) configure_host_metrics && return 0 ;;
      4) configure_geoip && return 0 ;;
      5) configure_security_traffic && return 0 ;;
      6) configure_xui_api && return 0 ;;
      *) printf '无效选择。\n' ;;
    esac
    pause
  done
}

update_collector() {
  printf '正在拉取 Alloy 最新镜像，采集器可能短暂重建。\n'
  compose pull
  compose up -d
  printf '采集器更新完成。\n'
}

uninstall_keep_data() {
  if ! confirm "停止采集器并删除配置，同时保留 Docker 数据卷？"; then
    printf '已取消。\n'
    return
  fi
  [ -f "$COMPOSE_FILE" ] && compose down
  rm -rf "$STACK_DIR"
  printf '已删除采集器容器和配置，Alloy 数据卷已保留。\n'
}

uninstall_everything() {
  if ! confirm "永久删除采集器、数据卷和项目镜像？"; then
    printf '已取消。\n'
    return
  fi
  [ -f "$COMPOSE_FILE" ] && compose down -v || true
  docker volume ls -q --filter label=com.docker.compose.project=xray-alloy-collector | xargs -r docker volume rm
  docker image rm grafana/alloy:latest python:3.12-alpine 2>/dev/null || true
  rm -rf "$STACK_DIR"
  rm -f "$0"
  printf 'Alloy 采集器已完整卸载。\n'
  exit 0
}

while true; do
  clear
  show_header
  cat <<'MENU'

0. 退出
1. 安装或更新脚本并重新部署
2. 启动采集器
3. 配置采集模块
4. 停止采集器
5. 重启采集器
6. 查看采集器状态与磁盘占用
7. 查看采集器日志
8. 查看采集器设置
9. 更新 Alloy
10. 卸载但保留采集器数据
11. 完整卸载并删除所有数据
MENU
  read -rp "请输入操作编号 [0-11]: " choice
  case "$choice" in
    0) exit 0 ;;
    1) update_script_and_deploy; exit $? ;;
    2) compose up -d; pause ;;
    3)
      if configure_modules; then
        exit 0
      else
        pause
      fi
      ;;
    4) compose stop; pause ;;
    5) compose restart; pause ;;
    6) show_status; pause ;;
    7) show_logs; pause ;;
    8) show_config; pause ;;
    9) update_collector; pause ;;
    10) uninstall_keep_data; pause ;;
    11) uninstall_everything ;;
    *) printf '无效选择。\n'; pause ;;
  esac
done
EOF
  chmod 0750 "$MANAGER_PATH"
}

show_installer_menu() {
  while true; do
    clear
    cat <<'MENU'
GLA Alloy 模块化采集器管理

0. 退出
1. 安装或更新采集器
2. 启动采集器
3. 停止采集器
4. 重启采集器
5. 查看采集器状态与磁盘占用
6. 查看 Alloy 日志
7. 查看采集器设置
8. 更新 Alloy
9. 卸载但保留采集器数据
10. 完整卸载并删除所有数据
MENU
    read -rp "请输入操作编号 [0-10]: " choice
    case "$choice" in
      0) exit 0 ;;
      1) install_collector; return ;;
      *) printf '请先选择 1 安装或更新采集器；安装完成后输入 alloy 使用完整管理功能。\n'; read -rp "按 Enter 键继续..." _ ;;
    esac
  done
}

case "${ALLOY_ACTION:-menu}" in
  install) install_collector ;;
  menu) show_installer_menu ;;
  *) die "未知操作：${ALLOY_ACTION}" ;;
esac
