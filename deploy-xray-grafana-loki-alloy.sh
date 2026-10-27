#!/usr/bin/env bash
# Deploys the central GLA observability stack.
set -Eeuo pipefail

STACK_DIR="${STACK_DIR:-/opt/xray-log-dashboard}"
ENABLE_GEOIP="${ENABLE_GEOIP:-auto}"
ENABLE_ASN="${ENABLE_ASN:-auto}"
ENABLE_SECURITY_TRAFFIC="${ENABLE_SECURITY_TRAFFIC:-auto}"
SSH_PORT="${SSH_PORT:-}"
GRAFANA_BIND="${GRAFANA_BIND:-0.0.0.0}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
LOKI_RETENTION="${LOKI_RETENTION:-168h}"
METRICS_RETENTION="${METRICS_RETENTION:-14d}"
SERVER_NAME="${SERVER_NAME:-central}"
NPM_NETWORK="${NPM_NETWORK:-npm-loki}"
ASSET_BASE_URL="${ASSET_BASE_URL:-https://raw.githubusercontent.com/xhpx7301/gla/main}"
XUI_API_URL="${XUI_API_URL:-}"
XUI_API_TOKEN="${XUI_API_TOKEN:-}"
XUI_LOCAL_HOST="${XUI_LOCAL_HOST:-}"
MANAGER_PATH="/usr/local/bin/gla"
INSTALL_SETTINGS_FILE="$STACK_DIR/.install.env"
GEOIP_DB_PATH="${GEOIP_DB_PATH:-$STACK_DIR/geoip/GeoLite2-City.mmdb}"
GEOIP_MIRROR_URL="${GEOIP_MIRROR_URL:-https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-City.mmdb}"
ASN_DB_PATH="${ASN_DB_PATH:-$STACK_DIR/geoip/GeoLite2-ASN.mmdb}"
ASN_MIRROR_URL="${ASN_MIRROR_URL:-https://raw.githubusercontent.com/P3TERX/GeoLite.mmdb/download/GeoLite2-ASN.mmdb}"

die() { printf '错误: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }

write_install_settings() {
  {
    printf 'STACK_DIR=%q\n' "$STACK_DIR"
    printf 'ENABLE_GEOIP=%q\n' "$ENABLE_GEOIP"
    printf 'ENABLE_ASN=%q\n' "$ENABLE_ASN"
    printf 'ENABLE_SECURITY_TRAFFIC=%q\n' "$ENABLE_SECURITY_TRAFFIC"
    printf 'SSH_PORT=%q\n' "$SSH_PORT"
    printf 'GEOIP_DB_PATH=%q\n' "$GEOIP_DB_PATH"
    printf 'GEOIP_MIRROR_URL=%q\n' "$GEOIP_MIRROR_URL"
    printf 'ASN_DB_PATH=%q\n' "$ASN_DB_PATH"
    printf 'ASN_MIRROR_URL=%q\n' "$ASN_MIRROR_URL"
    printf 'GRAFANA_BIND=%q\n' "$GRAFANA_BIND"
    printf 'GRAFANA_PORT=%q\n' "$GRAFANA_PORT"
    printf 'LOKI_RETENTION=%q\n' "$LOKI_RETENTION"
    printf 'METRICS_RETENTION=%q\n' "$METRICS_RETENTION"
    printf 'SERVER_NAME=%q\n' "$SERVER_NAME"
    printf 'NPM_NETWORK=%q\n' "$NPM_NETWORK"
    printf 'XUI_LOCAL_HOST=%q\n' "$XUI_LOCAL_HOST"
  } >"$INSTALL_SETTINGS_FILE"
  chmod 0600 "$INSTALL_SETTINGS_FILE"
}

require_install_prerequisites() {
  [ "$(id -u)" -eq 0 ] || die "请使用 root 执行：sudo bash $0"
  case "$ENABLE_GEOIP" in
    auto) [ -r "$GEOIP_DB_PATH" ] && ENABLE_GEOIP=true || ENABLE_GEOIP=false ;;
    true) [ -r "$GEOIP_DB_PATH" ] || die "已要求启用 GeoIP，但无法读取数据库：$GEOIP_DB_PATH" ;;
    false) ;;
    *) die "ENABLE_GEOIP 只能是 auto、true 或 false。" ;;
  esac
  case "$ENABLE_SECURITY_TRAFFIC" in
    auto)
      if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active$' && command -v iptables >/dev/null 2>&1 && command -v systemctl >/dev/null 2>&1; then
        ENABLE_SECURITY_TRAFFIC=true
      else
        ENABLE_SECURITY_TRAFFIC=false
      fi
      ;;
    true)
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
  [[ "$SERVER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die "SERVER_NAME 只能包含字母、数字、点、下划线和连字符。"
  if [ -n "$XUI_API_URL" ]; then
    [[ "$XUI_API_URL" =~ ^https://.+/panel/api/inbounds/list$ ]] || die "XUI_API_URL 必须是 HTTPS 且以 /panel/api/inbounds/list 结尾。"
    case "$XUI_API_URL" in *$'\n'*|*$'\r'*|*\"*) die "XUI_API_URL 包含不支持的字符。" ;; esac
    XUI_API_HOST="${XUI_API_URL#https://}"
    XUI_API_HOST="${XUI_API_HOST%%/*}"
    XUI_API_HOST="${XUI_API_HOST%%:*}"
    XUI_LOCAL_HOST="$XUI_API_HOST"
    [[ "$XUI_API_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || die "无法从 XUI_API_URL 识别面板域名。"
  fi
  if [ -n "$XUI_LOCAL_HOST" ]; then
    [[ "$XUI_LOCAL_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || die "本机面板域名格式无效。"
  fi
  command -v docker >/dev/null 2>&1 || die "未找到 Docker。通常 Nginx Proxy Manager 已经安装 Docker。"
  docker info >/dev/null 2>&1 || die "Docker 服务未运行或当前用户无权限访问。"

  if docker compose version >/dev/null 2>&1; then
    COMPOSE=(docker compose)
  elif command -v docker-compose >/dev/null 2>&1; then
    COMPOSE=(docker-compose)
  else
    die "需要 Docker Compose（docker compose 或 docker-compose）。"
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
    install -m 0644 "$GLA_ASSET_DIR/$relative_path" "$destination"
  elif command -v wget >/dev/null 2>&1; then
    wget -qO "$destination" "$ASSET_BASE_URL/$relative_path"
  elif command -v curl >/dev/null 2>&1; then
    curl -fsSL "$ASSET_BASE_URL/$relative_path" -o "$destination"
  else
    die "需要 wget 或 curl 下载仪表盘文件。"
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

install_asn_database() {
  local source_path="$1"
  [ -r "$source_path" ] || die "无法读取 ASN 数据库文件：$source_path"
  install -d -m 0750 "$STACK_DIR/geoip"
  install -m 0640 "$source_path" "$STACK_DIR/geoip/GeoLite2-ASN.mmdb"
  ASN_DB_PATH="$STACK_DIR/geoip/GeoLite2-ASN.mmdb"
  printf 'ASN 数据库已放置并启用：%s\n' "$ASN_DB_PATH"
}

download_asn_database() {
  local tmp_dir db_file http_code size_bytes
  command -v curl >/dev/null 2>&1 || die "从 GitHub 镜像下载需要 curl。请先安装 curl，或选择使用已有文件。"
  tmp_dir="$(mktemp -d)"
  db_file="$tmp_dir/GeoLite2-ASN.mmdb"
  http_code="$(curl -sSL --retry 2 "$ASN_MIRROR_URL" -o "$db_file" -w '%{http_code}')" || { rm -rf "$tmp_dir"; die "无法连接 ASN 镜像，请检查网络。"; }
  case "$http_code" in
    200) ;;
    404) rm -rf "$tmp_dir"; die "ASN 镜像中未找到 GeoLite2-ASN.mmdb。" ;;
    429) rm -rf "$tmp_dir"; die "GitHub 请求过于频繁，请稍后再试。" ;;
    *) rm -rf "$tmp_dir"; die "ASN 镜像下载失败，HTTP 状态码：$http_code" ;;
  esac
  size_bytes="$(wc -c <"$db_file")"
  [ "$size_bytes" -gt 1048576 ] || { rm -rf "$tmp_dir"; die "下载文件过小，可能不是有效的 GeoLite2-ASN.mmdb。"; }
  install_asn_database "$db_file"
  rm -rf "$tmp_dir"
}

prepare_asn_database() {
  local source_path choice
  [ "$ENABLE_ASN" != false ] || return
  if [ -r "$ASN_DB_PATH" ]; then
    return
  fi
  printf '\n未检测到 ASN 数据库。\n'
  printf 'ASN 数据库可为客户端最近记录 IP 显示中国运营商或境外网络组织。\n\n'
  printf '1. 从 GitHub GeoLite.mmdb 镜像下载（无需 MaxMind 密钥）\n'
  printf '2. 使用服务器上已有的 GeoLite2-ASN.mmdb\n'
  printf '0. 跳过 ASN 归属解析\n'
  read -rp "请选择 [0-2]: " choice
  case "$choice" in
    1) download_asn_database ;;
    2)
      read -rp "已有 GeoLite2-ASN.mmdb 文件路径: " source_path
      [ -n "$source_path" ] || { printf '未提供 ASN 数据库，跳过 ASN 归属解析。\n'; return; }
      install_asn_database "$source_path"
      ;;
    0|"") printf '已跳过 ASN 归属解析。\n' ;;
    *) die "无效选择。" ;;
  esac
}

install_stack() {
  prepare_geoip_database
  prepare_asn_database
  require_install_prerequisites
  docker network inspect "$NPM_NETWORK" >/dev/null 2>&1 || docker network create "$NPM_NETWORK" >/dev/null

  if [ -d /var/log/journal ] && [ -n "$(find /var/log/journal -mindepth 1 -maxdepth 1 -print -quit 2>/dev/null)" ]; then
    JOURNAL_PATH_HOST=/var/log/journal
  elif [ -d /run/log/journal ]; then
    JOURNAL_PATH_HOST=/run/log/journal
  else
    die "未找到 systemd journal 目录，无法采集 SSH 安全日志。"
  fi

  if [ -n "$XUI_API_URL" ]; then
    if [ -z "$XUI_API_TOKEN" ] && [ -r "$STACK_DIR/secrets/xui-api-token" ]; then
      XUI_API_TOKEN="$(cat "$STACK_DIR/secrets/xui-api-token")"
    fi
    if [ -z "$XUI_API_TOKEN" ]; then
      read -rp "请输入 3x-ui API Token（输入可见）: " XUI_API_TOKEN
    fi
    [ -n "$XUI_API_TOKEN" ] || die "XUI_API_TOKEN 不能为空。"
    case "$XUI_API_TOKEN" in *$'\n'*|*$'\r'*) die "XUI_API_TOKEN 不能包含换行符。" ;; esac
  fi

  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  if [ "$mem_kb" -lt 1258291 ]; then
    printf '警告：此服务器内存低于 1.2 GB，Grafana 可能较慢。请保留 Swap，并停止不需要的服务。\n' >&2
  fi

note "正在创建部署文件：$STACK_DIR"
install -d -m 0750 "$STACK_DIR" "$STACK_DIR/alloy" \
  "$STACK_DIR/loki" "$STACK_DIR/grafana/provisioning/datasources" \
  "$STACK_DIR/grafana/provisioning/dashboards" "$STACK_DIR/grafana/dashboards" \
  "$STACK_DIR/assets" "$STACK_DIR/secrets" "$STACK_DIR/geoip"
download_asset assets/xui_exporter.py "$STACK_DIR/assets/xui_exporter.py"
download_asset assets/xui-exporter.Dockerfile "$STACK_DIR/assets/xui-exporter.Dockerfile"
[ -f "$STACK_DIR/secrets/xui-panels.json" ] || printf '[]\n' >"$STACK_DIR/secrets/xui-panels.json"
[ -f "$STACK_DIR/secrets/ssh-login-config.json" ] || printf '{"servers":{}}\n' >"$STACK_DIR/secrets/ssh-login-config.json"
chmod 0600 "$STACK_DIR/secrets/xui-panels.json"
chmod 0600 "$STACK_DIR/secrets/ssh-login-config.json"
# Migrate an existing local-panel URL and Token into the shared API panel list.
# Once migrated, removing the panel in the manager also stops its collection.
if [ -n "$XUI_API_URL" ]; then
  printf '%s' "$XUI_API_TOKEN" | docker run --rm -i --network none \
    -e XUI_PANELS_FILE=/run/secrets/xui-panels.json \
    -v "$STACK_DIR/secrets:/run/secrets" \
    -v "$STACK_DIR/assets/xui_exporter.py:/app/xui_exporter.py:ro" \
    python:3.12-alpine python3 /app/xui_exporter.py panels migrate \
      --name "$SERVER_NAME" --url "$XUI_API_URL" --logs true
fi
write_install_settings
download_asset assets/security_traffic_collector.sh "$STACK_DIR/assets/security_traffic_collector.sh"
chmod 0750 "$STACK_DIR/assets/security_traffic_collector.sh"
setup_security_traffic_collector
printf 'COMPOSE_PROFILES=xui\n' >"$STACK_DIR/.env"
chmod 0600 "$STACK_DIR/.env"

XUI_SCRAPE_CONFIG="$(cat <<EOF

prometheus.scrape "xui" {
  targets = [{
    __address__ = "xui-exporter:9105",
    job         = "xui-metrics",
    server      = "${SERVER_NAME}",
  }]
  scrape_interval = "30s"
  honor_labels    = true
  forward_to      = [prometheus.remote_write.local.receiver]
}
EOF
)"

XUI_EXTRA_HOST_LINE=""
if [ -n "$XUI_LOCAL_HOST" ]; then
  XUI_EXTRA_HOST_LINE="    extra_hosts: [\"${XUI_LOCAL_HOST}:host-gateway\"]"
fi

GEOIP_VOLUME_LINE=""
XUI_CITY_VOLUME_LINE=""
if [ "$ENABLE_GEOIP" = true ]; then
  GEOIP_VOLUME_LINE="      - ${GEOIP_DB_PATH}:/var/lib/gla/geoip/GeoLite2-City.mmdb:ro"
  XUI_CITY_VOLUME_LINE="      - ${GEOIP_DB_PATH}:/var/lib/gla/geoip/GeoLite2-City.mmdb:ro"
fi
XUI_ASN_VOLUME_LINE=""
if [ -r "$ASN_DB_PATH" ] && [ "$ENABLE_ASN" != false ]; then
  XUI_ASN_VOLUME_LINE="      - ${ASN_DB_PATH}:/var/lib/gla/geoip/GeoLite2-ASN.mmdb:ro"
fi

GEOIP_XRAY_STAGES=""
GEOIP_SSH_STAGES=""
GEOIP_FAIL2BAN_STAGES=""
if [ "$ENABLE_GEOIP" = true ]; then
  GEOIP_XRAY_STAGES="$(cat <<'EOF'
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
)"
  GEOIP_SSH_STAGES="$GEOIP_XRAY_STAGES"
  GEOIP_FAIL2BAN_STAGES="$GEOIP_XRAY_STAGES"
fi

# Grafana only applies its admin password on the first database initialization.
# Preserve the generated value so a later script run cannot print a false password.
CREDENTIALS_FILE="$STACK_DIR/.credentials"
if [ -f "$CREDENTIALS_FILE" ]; then
  GRAFANA_ADMIN_PASSWORD="$(awk -F= '$1 == "GRAFANA_ADMIN_PASSWORD" {print substr($0, index($0, "=") + 1); exit}' "$CREDENTIALS_FILE")"
  # Older script versions wrote a literal "\\n" on each run. Remove any
  # accumulated suffix so the saved value matches the original password.
  while [[ "$GRAFANA_ADMIN_PASSWORD" == *\\n ]]; do
    GRAFANA_ADMIN_PASSWORD="${GRAFANA_ADMIN_PASSWORD%\\n}"
  done
  [ -n "$GRAFANA_ADMIN_PASSWORD" ] || die "凭据文件无效：$CREDENTIALS_FILE"
elif [ -z "${GRAFANA_ADMIN_PASSWORD:-}" ]; then
  GRAFANA_ADMIN_PASSWORD="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \\n')"
fi
printf 'GRAFANA_ADMIN_PASSWORD=%s\n' "$GRAFANA_ADMIN_PASSWORD" >"$CREDENTIALS_FILE"
chmod 0600 "$CREDENTIALS_FILE"

cat >"$STACK_DIR/compose.yaml" <<EOF
services:
  grafana:
    image: grafana/grafana:latest
    container_name: xray-grafana
    restart: unless-stopped
    ports:
      - "${GRAFANA_BIND}:${GRAFANA_PORT}:3000"
    environment:
      GF_SECURITY_ADMIN_USER: admin
      GF_SECURITY_ADMIN_PASSWORD: ${GRAFANA_ADMIN_PASSWORD}
      GF_USERS_DEFAULT_LANGUAGE: zh-Hans
      GF_AUTH_ANONYMOUS_ENABLED: "false"
      GF_SERVER_ROOT_URL: "%(protocol)s://%(domain)s:%(http_port)s/"
    volumes:
      - grafana-data:/var/lib/grafana
      - ./grafana/provisioning:/etc/grafana/provisioning:ro
      - ./grafana/dashboards:/var/lib/grafana/dashboards:ro
    depends_on:
      - loki

  loki:
    image: grafana/loki:latest
    container_name: xray-loki
    restart: unless-stopped
    command: -config.file=/etc/loki/config.yaml
    networks:
      - default
      - npm
    volumes:
      - ./loki/config.yaml:/etc/loki/config.yaml:ro
      - loki-data:/loki

  victoriametrics:
    image: victoriametrics/victoria-metrics:latest
    container_name: gla-victoriametrics
    restart: unless-stopped
    command:
      - -storageDataPath=/victoria-metrics-data
      - -retentionPeriod=${METRICS_RETENTION}
    networks:
      - default
      - npm
    volumes:
      - victoria-metrics-data:/victoria-metrics-data

  alloy:
    image: grafana/alloy:latest
    container_name: xray-alloy
    restart: unless-stopped
    user: "0:0"
    command: run --server.http.listen-addr=0.0.0.0:12345 --storage.path=/var/lib/alloy/data /etc/alloy/config.alloy
    volumes:
${GEOIP_VOLUME_LINE}
      - ${JOURNAL_PATH_HOST}:/var/log/journal:ro
      - /etc/machine-id:/etc/machine-id:ro
      - /var/log:/host/var/log:ro
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/host/root:ro,rslave
${SECURITY_TRAFFIC_VOLUME_LINE}
      - ./alloy/config.alloy:/etc/alloy/config.alloy:ro
      - alloy-data:/var/lib/alloy/data
    depends_on:
      - loki
      - victoriametrics

  xui-exporter:
    profiles: ["xui"]
    build:
      context: ./assets
      dockerfile: xui-exporter.Dockerfile
    image: gla-xui-exporter:local
    container_name: gla-xui-exporter
    restart: unless-stopped
    command: ["python3", "/app/xui_exporter.py"]
    environment:
      SERVER_NAME: "${SERVER_NAME}"
      XUI_PANELS_FILE: /run/secrets/xui-panels.json
      XUI_STATE_DIR: /var/lib/xui
      XUI_LOKI_URL: http://alloy:3500/loki/api/v1/push
      XUI_TELEGRAM_CONFIG_FILE: /run/secrets/telegram-config.json
      XUI_SSH_LOGIN_CONFIG_FILE: /run/secrets/ssh-login-config.json
      GEOIP_CITY_DB: /var/lib/gla/geoip/GeoLite2-City.mmdb
      GEOIP_ASN_DB: /var/lib/gla/geoip/GeoLite2-ASN.mmdb
${XUI_EXTRA_HOST_LINE}
    volumes:
      - ./assets/xui_exporter.py:/app/xui_exporter.py:ro
${XUI_CITY_VOLUME_LINE}
${XUI_ASN_VOLUME_LINE}
      - ./secrets:/run/secrets:ro
      - xui-state:/var/lib/xui
    depends_on:
      - alloy
    networks:
      - default
      - npm

volumes:
  grafana-data:
  loki-data:
  victoria-metrics-data:
  alloy-data:
  xui-state:

networks:
  npm:
    external: true
    name: ${NPM_NETWORK}
EOF

cat >"$STACK_DIR/loki/config.yaml" <<EOF
auth_enabled: false

server:
  http_listen_port: 3100

common:
  path_prefix: /loki
  replication_factor: 1
  ring:
    kvstore:
      store: inmemory
  storage:
    filesystem:
      chunks_directory: /loki/chunks
      rules_directory: /loki/rules

schema_config:
  configs:
    - from: 2024-01-01
      store: tsdb
      object_store: filesystem
      schema: v13
      index:
        prefix: index_
        period: 24h

limits_config:
  retention_period: ${LOKI_RETENTION}
  allow_structured_metadata: false

compactor:
  working_directory: /loki/compactor
  retention_enabled: true
  delete_request_store: filesystem

analytics:
  reporting_enabled: false
EOF

cat >"$STACK_DIR/alloy/config.alloy" <<EOF
loki.source.api "xui_access" {
  http {
    listen_address = "0.0.0.0"
    listen_port    = 3500
  }
  forward_to = [loki.process.xui_access.receiver]
}

loki.process "xui_access" {
  forward_to = [loki.write.default.receiver]
  stage.regex {
    expression = "from \\\\[?(?P<source_ip>[0-9A-Fa-f:.]+)\\\\]?:[0-9]+ accepted"
  }
${GEOIP_XRAY_STAGES}
}

EOF
cat >>"$STACK_DIR/alloy/config.alloy" <<EOF
loki.source.journal "ssh" {
  path       = "/var/log/journal"
  matches    = "_SYSTEMD_UNIT=ssh.service"
  labels     = { job = "ssh-journal", server = "${SERVER_NAME}" }
  forward_to = [loki.process.ssh.receiver]
}

loki.source.journal "sshd" {
  path       = "/var/log/journal"
  matches    = "_SYSTEMD_UNIT=sshd.service"
  labels     = { job = "ssh-journal", server = "${SERVER_NAME}" }
  forward_to = [loki.process.ssh.receiver]
}

loki.process "ssh" {
  forward_to = [loki.write.default.receiver]

  stage.regex {
    expression = "from (?P<source_ip>(?:\\\\[[0-9A-Fa-f:]+\\\\]|[0-9A-Fa-f:.]+))"
  }

${GEOIP_SSH_STAGES}
}

local.file_match "fail2ban" {
  path_targets = [{
    __path__ = "/host/var/log/fail2ban.log",
    job      = "fail2ban",
    server   = "${SERVER_NAME}",
  }]
}

loki.source.file "fail2ban" {
  targets    = local.file_match.fail2ban.targets
  forward_to = [loki.process.fail2ban.receiver]
}

loki.process "fail2ban" {
  forward_to = [loki.write.default.receiver]

  stage.regex {
    expression = "^(?P<timestamp>\\\\d{4}-\\\\d{2}-\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2},\\\\d+).*?(?:Found|Ban|Unban) (?P<source_ip>(?:\\\\[[0-9A-Fa-f:]+\\\\]|[0-9A-Fa-f:.]+))"
  }

${GEOIP_FAIL2BAN_STAGES}

  stage.timestamp {
    source = "timestamp"
    format = "2006-01-02 15:04:05,000"
    location = "Asia/Shanghai"
  }
}

local.file_match "ufw" {
  path_targets = [{
    __path__ = "/host/var/log/ufw.log",
    job      = "ufw",
    server   = "${SERVER_NAME}",
  }]
}

loki.source.file "ufw" {
  targets    = local.file_match.ufw.targets
  forward_to = [loki.write.default.receiver]
}

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
    replacement  = "${SERVER_NAME}"
  }
}

prometheus.scrape "host" {
  targets         = discovery.relabel.host.output
  scrape_interval = "30s"
  forward_to      = [prometheus.remote_write.local.receiver]
}

${XUI_SCRAPE_CONFIG}

prometheus.remote_write "local" {
  endpoint {
    url = "http://victoriametrics:8428/api/v1/write"
  }
}

loki.write "default" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
EOF

cat >"$STACK_DIR/grafana/provisioning/datasources/loki.yaml" <<'EOF'
apiVersion: 1

datasources:
  - name: Xray Loki
    uid: loki
    type: loki
    access: proxy
    url: http://loki:3100
    isDefault: true
    editable: false
  - name: VictoriaMetrics
    uid: victoriametrics
    type: prometheus
    access: proxy
    url: http://victoriametrics:8428
    isDefault: false
    editable: false
EOF

cat >"$STACK_DIR/grafana/provisioning/dashboards/xray.yaml" <<'EOF'
apiVersion: 1

providers:
  - name: Xray
    folder: Xray
    type: file
    disableDeletion: true
    editable: true
    options:
      path: /var/lib/grafana/dashboards
EOF

note "正在安装 Xray 和服务器安全仪表盘"
download_asset dashboards/xray-gateway.json "$STACK_DIR/grafana/dashboards/xray-gateway.json"
download_asset dashboards/server-security.json "$STACK_DIR/grafana/dashboards/server-security.json"
download_asset dashboards/ssh-login.json "$STACK_DIR/grafana/dashboards/ssh-login.json"

cat >"$STACK_DIR/grafana/dashboards/xray-access.json" <<'EOF'
{
  "uid": "xray-access",
  "title": "Xray 访问日志",
  "timezone": "Asia/Shanghai",
  "schemaVersion": 39,
  "version": 16,
  "refresh": "30s",
  "time": { "from": "now-1h", "to": "now" },
  "templating": {
    "list": [
      {
        "name": "server",
        "label": "服务器",
        "type": "query",
        "datasource": { "type": "loki", "uid": "loki" },
        "definition": "label_values({job=\"xray-access\", server=~\".+\"}, server)",
        "query": "label_values({job=\"xray-access\", server=~\".+\"}, server)",
        "refresh": 1,
        "includeAll": true,
        "allValue": ".*",
        "multi": false,
        "options": [],
        "current": { "selected": true, "text": "全部", "value": "$__all" }
      },
      {"name": "group", "label": "客户端分组", "type": "query", "datasource": {"type": "loki", "uid": "loki"}, "definition": "label_values({job=\"xray-access\",server=~\"$server\",group=~\".+\"}, group)", "query": "label_values({job=\"xray-access\",server=~\"$server\",group=~\".+\"}, group)", "refresh": 2, "includeAll": true, "allValue": ".*", "multi": true, "options": [], "sort": 1, "current": {"selected": true, "text": "全部", "value": "$__all"}},
      {
        "name": "client",
        "label": "客户端",
        "type": "query",
        "datasource": { "type": "loki", "uid": "loki" },
        "definition": "label_values({job=\"xray-access\", server=~\"$server\", group=~\"$group\", email=~\".+\"}, email)",
        "query": "label_values({job=\"xray-access\", server=~\"$server\", group=~\"$group\", email=~\".+\"}, email)",
        "refresh": 1,
        "includeAll": true,
        "allValue": ".*",
        "multi": false,
        "options": [],
        "current": { "selected": true, "text": "全部", "value": "$__all" }
      },
      {
        "name": "site",
        "label": "访问目标关键词（域名/IP）",
        "type": "textbox",
        "hide": 0,
        "query": "",
        "options": [
          { "selected": true, "text": "", "value": "" }
        ],
        "current": { "selected": true, "text": "", "value": "" }
      },
      {
        "name": "period",
        "label": "统计周期",
        "type": "custom",
        "hide": 0,
        "query": "1d,7d,30d",
        "options": [
          { "selected": false, "text": "1 天", "value": "1d" },
          { "selected": true, "text": "7 天", "value": "7d" },
          { "selected": false, "text": "30 天", "value": "30d" }
        ],
        "current": { "selected": true, "text": "7 天", "value": "7d" }
      }
    ]
  },
  "panels": [
    {
      "id": 1,
      "type": "logs",
      "title": "全部已采集访问记录",
      "description": "访问记录统一来自 3x-ui API，返回解析记录且排除 api -> api；日志可能因轮转或采集窗口不足而缺失。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "{job=\"xray-access\", server=~\"$server\", group=~\"$group\"}" }],
      "gridPos": { "x": 0, "y": 42, "w": 24, "h": 10 },
      "options": { "dedupStrategy": "none", "enableLogDetails": true, "showCommonLabels": false, "wrapLogMessage": true, "sortOrder": "Descending" }
    },
    {
      "id": 2,
      "type": "logs",
      "title": "真实访问日志（已排除 API）",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "{job=\"xray-access\", server=~\"$server\", group=~\"$group\"} != \"[api -> api]\"" }],
      "gridPos": { "x": 0, "y": 32, "w": 24, "h": 10 },
      "options": { "dedupStrategy": "none", "enableLogDetails": true, "showCommonLabels": false, "wrapLogMessage": true, "sortOrder": "Descending" }
    },
    {
      "id": 3,
      "type": "logs",
      "title": "所选客户端的访问记录",
      "description": "选择客户端后，可在顶部“访问目标关键词”输入域名或 IP；留空则显示该客户端全部访问记录。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "{job=\"xray-access\", server=~\"$server\", group=~\"$group\", email=~\"$client\"} != \"[api -> api]\" |= \"$site\"" }],
      "gridPos": { "x": 0, "y": 0, "w": 16, "h": 9 },
      "options": { "dedupStrategy": "none", "enableLogDetails": true, "showCommonLabels": false, "wrapLogMessage": true, "sortOrder": "Descending" }
    },
    {
      "id": 4,
      "type": "stat",
      "title": "匹配连接数",
      "description": "统计所选客户端和访问目标关键词在当前时间范围内匹配到的 Xray 连接记录数。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "sum(count_over_time({job=\"xray-access\", server=~\"$server\", group=~\"$group\", email=~\"$client\"} != \"[api -> api]\" |= \"$site\" [$__range]))", "instant": true }],
      "gridPos": { "x": 16, "y": 0, "w": 8, "h": 4 },
      "options": { "reduceOptions": { "values": false, "calcs": ["lastNotNull"], "fields": "" }, "orientation": "auto", "textMode": "auto", "colorMode": "value", "graphMode": "none", "justifyMode": "auto" }
    },
    {
      "id": 8,
      "type": "logs",
      "title": "最新访问日志",
      "description": "显示 Loki 收到的最新一条真实访问记录。没有新访问不代表采集异常。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "{job=\"xray-access\", server=~\"$server\", group=~\"$group\"} != \"[api -> api]\"", "maxLines": 1 }],
      "gridPos": { "x": 16, "y": 4, "w": 8, "h": 5 },
      "options": { "dedupStrategy": "none", "enableLogDetails": false, "showCommonLabels": false, "wrapLogMessage": false, "sortOrder": "Descending" }
    },
    {
      "id": 5,
      "type": "table",
      "title": "所选客户端近 $period 访问目标 Top 20",
      "description": "按目标域名或 IP 聚合。选择“客户端”和“统计周期”后自动更新。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "topk(20, sum by (destination) (count_over_time({job=\"xray-access\", server=~\"$server\", group=~\"$group\", email=~\"$client\"} != \"[api -> api]\" | pattern `<_> accepted <_>:<destination>:<port> [<_>` [$period])))", "instant": true, "format": "table" }],
      "gridPos": { "x": 0, "y": 9, "w": 24, "h": 9 },
      "fieldConfig": {
        "defaults": {},
        "overrides": [
          { "matcher": { "id": "byName", "options": "destination" }, "properties": [{ "id": "custom.align", "value": "left" }] },
          { "matcher": { "id": "byName", "options": "Value #A" }, "properties": [{ "id": "custom.width", "value": 100 }, { "id": "custom.align", "value": "right" }] }
        ]
      },
      "options": { "showHeader": true },
      "transformations": [
        {
          "id": "sortBy",
          "options": {
            "fields": {},
            "sort": [{ "field": "Value #A", "desc": true }]
          }
        },
        {
          "id": "organize",
          "options": {
            "excludeByName": { "Time": true },
            "indexByName": { "destination": 0, "Value #A": 1 },
            "renameByName": { "destination": "访问目标", "Value #A": "次数" }
          }
        }
      ]
    },
    {
      "id": 6,
      "type": "table",
      "title": "按客户端连接数",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "sum by (email) (count_over_time({job=\"xray-access\", server=~\"$server\", group=~\"$group\", email=~\".+\"} != \"[api -> api]\" [$__range]))", "instant": true, "format": "table" }],
      "gridPos": { "x": 0, "y": 18, "w": 24, "h": 7 },
      "fieldConfig": {
        "defaults": {},
        "overrides": [
          { "matcher": { "id": "byName", "options": "email" }, "properties": [{ "id": "custom.align", "value": "left" }] },
          { "matcher": { "id": "byName", "options": "Value #A" }, "properties": [{ "id": "custom.width", "value": 100 }, { "id": "custom.align", "value": "right" }] }
        ]
      },
      "options": { "showHeader": true },
      "transformations": [
        { "id": "sortBy", "options": { "sort": [{ "field": "Value #A", "desc": true }] } },
        {
          "id": "organize",
          "options": {
            "excludeByName": { "Time": true },
            "indexByName": { "email": 0, "Value #A": 1 },
            "renameByName": { "email": "客户端", "Value #A": "次数" }
          }
        }
      ]
    },
    {
      "id": 7,
      "type": "table",
      "title": "按入站连接数",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "sum by (inbound) (count_over_time({job=\"xray-access\", server=~\"$server\", group=~\"$group\", inbound=~\".+\"} != \"[api -> api]\" [$__range]))", "instant": true, "format": "table" }],
      "gridPos": { "x": 0, "y": 25, "w": 24, "h": 7 },
      "fieldConfig": {
        "defaults": {},
        "overrides": [
          { "matcher": { "id": "byName", "options": "inbound" }, "properties": [{ "id": "custom.align", "value": "left" }] },
          { "matcher": { "id": "byName", "options": "Value #A" }, "properties": [{ "id": "custom.width", "value": 100 }, { "id": "custom.align", "value": "right" }] }
        ]
      },
      "options": { "showHeader": true },
      "transformations": [
        { "id": "sortBy", "options": { "sort": [{ "field": "Value #A", "desc": true }] } },
        {
          "id": "organize",
          "options": {
            "excludeByName": { "Time": true },
            "indexByName": { "inbound": 0, "Value #A": 1 },
            "renameByName": { "inbound": "入站", "Value #A": "次数" }
          }
        }
      ]
    },
    {"id": 21, "type": "stat", "title": "API 日志采集状态", "datasource": {"type": "prometheus", "uid": "victoriametrics"}, "targets": [{"refId": "A", "expr": "min(xui_access_log_up{server=~\"$server\"} and on(server) (xui_access_log_enabled{server=~\"$server\"} == 1))", "instant": true}], "gridPos": {"x": 0, "y": 52, "w": 8, "h": 5}, "fieldConfig": {"defaults": {"unit": "none", "noValue": "未启用或尚未成功采集", "mappings": [{"type": "value", "options": {"0": {"text": "失败或等待采集", "color": "red"}, "1": {"text": "正常", "color": "green"}}}]}, "overrides": []}, "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "graphMode": "none"}},
    {"id": 22, "type": "stat", "title": "API 日志最近成功采集", "datasource": {"type": "prometheus", "uid": "victoriametrics"}, "targets": [{"refId": "A", "expr": "1000 * min(xui_access_log_last_success_timestamp_seconds{server=~\"$server\"} > 0)", "instant": true}], "gridPos": {"x": 8, "y": 52, "w": 8, "h": 5}, "fieldConfig": {"defaults": {"unit": "dateTimeAsLocal", "noValue": "未启用或尚未成功采集"}, "overrides": []}, "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "graphMode": "none"}},
    {"id": 23, "type": "stat", "title": "API 日志窗口风险", "datasource": {"type": "prometheus", "uid": "victoriametrics"}, "targets": [{"refId": "A", "expr": "max(xui_access_log_window_full{server=~\"$server\"} + xui_access_log_window_gap{server=~\"$server\"})", "instant": true}], "gridPos": {"x": 16, "y": 52, "w": 8, "h": 5}, "fieldConfig": {"defaults": {"unit": "none", "noValue": "未启用或尚未成功采集", "mappings": [{"type": "value", "options": {"0": {"text": "正常", "color": "green"}, "1": {"text": "窗口已满", "color": "yellow"}, "2": {"text": "可能漏采", "color": "red"}}}]}, "overrides": []}, "options": {"reduceOptions": {"calcs": ["lastNotNull"], "fields": "", "values": false}, "graphMode": "none"}, "description": "0：窗口未满；1：窗口已满，建议增大条数；2：窗口已满且与上次没有重叠，可能漏采。接口不保证完整归档。"}
  ]
}
EOF

chmod 0600 "$STACK_DIR/compose.yaml"

note "正在启动 Grafana、Loki、VictoriaMetrics 和 Alloy"
cd "$STACK_DIR"
"${COMPOSE[@]}" pull grafana loki victoriametrics alloy
"${COMPOSE[@]}" build xui-exporter
"${COMPOSE[@]}" up -d
"${COMPOSE[@]}" restart alloy
"${COMPOSE[@]}" restart xui-exporter

note "正在验证容器状态"
"${COMPOSE[@]}" ps

install_manager

cat <<EOF

部署完成。

Grafana 地址：http://服务器_IP:${GRAFANA_PORT}
Grafana 用户名：admin
Grafana 密码：${GRAFANA_ADMIN_PASSWORD}
Xray 数据采集：中心端统一通过 3x-ui API 获取

安全提示：端口 ${GRAFANA_PORT} 已监听在所有服务器网络接口。
公网使用前，请在云防火墙或服务器防火墙中仅允许你的管理 IP 访问。

管理命令：
  gla
EOF
}

install_manager() {
  cat >"$MANAGER_PATH" <<'EOF'
#!/usr/bin/env bash
set -Eeuo pipefail

GLA_VERSION="3.0.0"
STACK_DIR="${STACK_DIR:-/opt/xray-log-dashboard}"
COMPOSE_FILE="$STACK_DIR/compose.yaml"
INSTALL_SETTINGS_FILE="$STACK_DIR/.install.env"
INSTALLER_URL="https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-grafana-loki-alloy.sh"

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

format_service_log_utc8() {
  local line timestamp converted prefix remainder
  while IFS= read -r line; do
    timestamp="$(printf '%s\n' "$line" | sed -n 's/.*\([0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9:.]*Z\).*/\1/p')"
    if [ -n "$timestamp" ] && converted="$(TZ=UTC-8 date -d "$timestamp" '+%Y-%m-%d %H:%M:%S UTC+8' 2>/dev/null)"; then
      prefix="${line%%"$timestamp"*}"
      remainder="${line#*"$timestamp"}"
      printf '%s%s%s\n' "$prefix" "$converted" "$remainder"
    else
      printf '%s\n' "$line"
    fi
  done
}

xui_state() {
  if ! grep -Eq '^COMPOSE_PROFILES=.*xui' "$STACK_DIR/.env" 2>/dev/null; then
    printf '[未启用]'
  else
    container_state gla-xui-exporter
  fi
}

show_header() {
  printf 'GLA %s - 轻量服务器观测平台\n\n' "$GLA_VERSION"
  printf 'Grafana 仪表盘展示        %s\n' "$(container_state xray-grafana)"
  printf 'Loki 日志存储             %s\n' "$(container_state xray-loki)"
  printf 'VictoriaMetrics 指标存储  %s\n' "$(container_state gla-victoriametrics)"
  printf 'Alloy 安全与资源采集      %s\n' "$(container_state xray-alloy)"
  printf '3x-ui API 流量与日志采集  %s\n' "$(xui_state)"
  printf 'SSH 登录告警             %s\n' "$(ssh_login_state)"
  if grep -Eq '^ENABLE_GEOIP=true$' "$STACK_DIR/.install.env" 2>/dev/null; then
    printf 'GeoIP 归属解析            [已启用]\n'
  else
    printf 'GeoIP 归属解析            [未启用]\n'
  fi
}

compose() {
  [ -f "$COMPOSE_FILE" ] || die "日志面板未安装：$STACK_DIR"
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
    printf '未找到面板配置。可能仍保留 Docker 数据卷。\n'
  fi

  local image volume mountpoint
  printf '\n项目镜像占用：\n'
  for image in grafana/grafana:latest grafana/loki:latest grafana/alloy:latest victoriametrics/victoria-metrics:latest python:3.12-alpine; do
    docker image ls "$image"
  done

  printf '\n项目数据卷占用：\n'
  while IFS= read -r volume; do
    [ -n "$volume" ] || continue
    mountpoint="$(docker volume inspect -f '{{.Mountpoint}}' "$volume")"
    du -sh "$mountpoint"
  done < <(docker volume ls -q --filter label=com.docker.compose.project=xray-log-dashboard)

}

show_logs() {
  printf '\n服务日志\n\n1. Grafana\n2. Loki\n3. VictoriaMetrics\n4. Alloy\n5. 3x-ui API 中心采集（流量与日志）\n0. 返回\n'
  read -rp "请选择服务: " choice
  case "$choice" in
    1) compose logs -f --tail=100 grafana 2>&1 | format_service_log_utc8 || true ;;
    2) compose logs -f --tail=100 loki 2>&1 | format_service_log_utc8 || true ;;
    3) compose logs -f --tail=100 victoriametrics 2>&1 | format_service_log_utc8 || true ;;
    4) compose logs -f --tail=100 alloy 2>&1 | format_service_log_utc8 || true ;;
    5)
      if grep -Eq '^COMPOSE_PROFILES=.*xui' "$STACK_DIR/.env" 2>/dev/null; then
        compose logs -f --tail=100 xui-exporter 2>&1 | format_service_log_utc8 || true
      else
        printf '3x-ui API 流量采集未启用。\n'
      fi
      ;;
    0) return ;;
    *) printf '无效选择。\n' ;;
  esac
}

service_control_menu() {
  while true; do
    clear
    show_header
    cat <<'MENU'

服务控制

0. 返回主菜单
1. 启动全部服务
2. 停止全部服务
3. 重启全部服务
4. 仅重启 Grafana
5. 仅重启 Loki
6. 仅重启 VictoriaMetrics
7. 仅重启 Alloy
8. 仅重启 3x-ui API 中心采集
MENU
    read -rp "请输入操作编号 [0-8]: " choice
    case "$choice" in
      0) return ;;
      1) compose up -d; pause ;;
      2)
        if confirm "停止全部观测服务？采集期间会暂时中断"; then compose stop; else printf '已取消。\n'; fi
        pause
        ;;
      3) compose restart; pause ;;
      4) compose restart grafana; pause ;;
      5) compose restart loki; pause ;;
      6) compose restart victoriametrics; pause ;;
      7) compose restart alloy; pause ;;
      8)
        if grep -Eq '^COMPOSE_PROFILES=.*xui' "$STACK_DIR/.env" 2>/dev/null; then
          compose restart xui-exporter
        else
          printf '3x-ui API 流量采集未启用。\n'
        fi
        pause
        ;;
      *) printf '无效选择。\n'; pause ;;
    esac
  done
}

show_access_info() {
  local grafana_bind="0.0.0.0" grafana_port="3000" grafana_password=""
  if [ -r "$INSTALL_SETTINGS_FILE" ]; then
    set +u
    # Generated by GLA and contains shell-escaped non-secret settings.
    . "$INSTALL_SETTINGS_FILE"
    set -u
    grafana_bind="${GRAFANA_BIND:-0.0.0.0}"
    grafana_port="${GRAFANA_PORT:-3000}"
  fi

  printf '\n访问与模块信息\n\n'
  if [ "$grafana_bind" = "0.0.0.0" ]; then
    printf 'Grafana：       http://服务器_IP:%s\n' "$grafana_port"
  else
    printf 'Grafana：       http://%s:%s\n' "$grafana_bind" "$grafana_port"
  fi
  printf 'Loki 内部地址： http://loki:3100\n'
  printf '指标内部地址： http://victoriametrics:8428\n'
  printf '远程指标路径： /api/v1/write（需通过 HTTPS 反向代理和认证）\n'

  if [ -f "$STACK_DIR/.credentials" ]; then
    grafana_password="$(sed -n 's/^GRAFANA_ADMIN_PASSWORD=//p' "$STACK_DIR/.credentials" | head -n 1)"
    printf 'Grafana 用户： admin\nGrafana 密码： %s\n' "$grafana_password"
  else
    printf 'Grafana 凭据： 未找到\n'
  fi

  printf '\n已配置仪表盘：\n'
  [ -f "$STACK_DIR/grafana/dashboards/xray-access.json" ] && printf '  - Xray 访问日志\n'
  [ -f "$STACK_DIR/grafana/dashboards/xray-gateway.json" ] && printf '  - Xray 网关流量与连接\n'
  [ -f "$STACK_DIR/grafana/dashboards/server-security.json" ] && printf '  - 服务器安全事件与系统资源\n'
  [ -f "$STACK_DIR/grafana/dashboards/ssh-login.json" ] && printf '  - SSH 登录记录\n'

  printf '\n中心端管理的 API 面板（Token 不显示）：\n'
  panels_command list
  printf '\nTelegram Bot：\n'
  telegram_command show || true
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
  local xui_local_host_is_set="${XUI_LOCAL_HOST+x}" xui_local_host_override="${XUI_LOCAL_HOST-}"
  local enable_geoip_is_set="${ENABLE_GEOIP+x}" enable_geoip_override="${ENABLE_GEOIP-}"
  local geoip_db_path_is_set="${GEOIP_DB_PATH+x}" geoip_db_path_override="${GEOIP_DB_PATH-}"
  local enable_asn_is_set="${ENABLE_ASN+x}" enable_asn_override="${ENABLE_ASN-}"
  local asn_db_path_is_set="${ASN_DB_PATH+x}" asn_db_path_override="${ASN_DB_PATH-}"
  [ -r "$INSTALL_SETTINGS_FILE" ] || die "未找到安装配置：$INSTALL_SETTINGS_FILE"
  printf '正在从 GitHub 下载最新版脚本并重新部署，服务可能短暂重建。\n'
  set -a
  # This file is created locally by the installer and contains no password.
  . "$INSTALL_SETTINGS_FILE"
  set +a
  if [ "$xui_local_host_is_set" = x ]; then
    XUI_LOCAL_HOST="$xui_local_host_override"
    export XUI_LOCAL_HOST
  fi
  if [ "$enable_geoip_is_set" = x ]; then
    ENABLE_GEOIP="$enable_geoip_override"
    export ENABLE_GEOIP
  else
    # Menu option 1 offers GeoIP setup again when the previous deployment skipped it.
    ENABLE_GEOIP=auto
    export ENABLE_GEOIP
  fi
  if [ "$geoip_db_path_is_set" = x ]; then
    GEOIP_DB_PATH="$geoip_db_path_override"
    export GEOIP_DB_PATH
  fi
  if [ "$enable_asn_is_set" = x ]; then
    ENABLE_ASN="$enable_asn_override"
    export ENABLE_ASN
  fi
  if [ "$asn_db_path_is_set" = x ]; then
    ASN_DB_PATH="$asn_db_path_override"
    export ASN_DB_PATH
  fi
  GLA_ACTION=install bash <(download_installer)
}

panels_command() {
  # Use the bundled container runtime; no host Python installation required.
  docker run --rm -i --network none \
    -e XUI_PANELS_FILE=/run/secrets/xui-panels.json \
    -v "$STACK_DIR/secrets:/run/secrets" \
    -v "$STACK_DIR/assets/xui_exporter.py:/app/xui_exporter.py:ro" \
    python:3.12-alpine python3 /app/xui_exporter.py panels "$@"
}

telegram_command() {
  docker run --rm -i --network none \
    -e XUI_TELEGRAM_CONFIG_FILE=/run/secrets/telegram-config.json \
    -v "$STACK_DIR/secrets:/run/secrets" \
    -v "$STACK_DIR/assets/xui_exporter.py:/app/xui_exporter.py:ro" \
    python:3.12-alpine python3 /app/xui_exporter.py telegram "$@"
}

ssh_login_command() {
  docker run --rm -i --network none \
    -e XUI_SSH_LOGIN_CONFIG_FILE=/run/secrets/ssh-login-config.json \
    -v "$STACK_DIR/secrets:/run/secrets" \
    -v "$STACK_DIR/assets/xui_exporter.py:/app/xui_exporter.py:ro" \
    python:3.12-alpine python3 /app/xui_exporter.py ssh-login "$@"
}

ssh_login_state() {
  if [ ! -f "$STACK_DIR/secrets/ssh-login-config.json" ]; then
    printf '[未配置]'
  else
    local count
    count="$(ssh_login_command status 2>/dev/null | cut -d'台' -f1 | awk '{print $1}')"
    printf '[%s 台已登记]' "${count:-0}"
  fi
}

configure_ssh_login() {
  local action name cidrs url
  while true; do
    printf '\nSSH 登录告警管理\n\n'
    ssh_login_command list || true
    printf '\n1. 设置或修改 HTTPS 接收地址\n2. 添加服务器并生成密钥\n3. 修改可信来源 IP\n4. 生成子服务器安装命令\n5. 删除服务器\n0. 返回\n'
    read -rp '请选择 [0-5]: ' action
    case "$action" in
      0) return 0 ;;
      1)
        read -rp 'HTTPS 接收地址（例 https://ssh.example.com/api/ssh-login）: ' url
        ssh_login_command set-url --url "$url"
        ;;
      2)
        read -rp '服务器名称（仅字母、数字、点、下划线、连字符）: ' name
        read -rp '可信登录来源 CIDR，多个用逗号分隔；留空表示全部告警: ' cidrs
        if [ -z "$cidrs" ] && ! confirm '未设置可信来源，所有成功登录都会触发告警。继续添加？'; then
          printf '已取消。\n'
        else
          ssh_login_command add --name "$name" --cidrs "$cidrs" && printf '服务器已添加，请用“生成子服务器安装命令”获取安装命令。\n'
        fi
        ;;
      3)
        read -rp '服务器名称: ' name
        read -rp '可信登录来源 CIDR，多个用逗号分隔；留空表示全部告警: ' cidrs
        if [ -z "$cidrs" ] && ! confirm '留空后所有成功登录都会触发告警。继续修改？'; then
          printf '已取消。\n'
        else
          ssh_login_command set-cidrs --name "$name" --cidrs "$cidrs"
        fi
        ;;
      4)
        read -rp '服务器名称: ' name
        printf '\n以下命令请在对应子服务器以 root 执行：\n'
        ssh_login_command install-command --name "$name"
        ;;
      5)
        read -rp '要删除的服务器名称: ' name
        if confirm "删除 $name 的告警配置？子服务器采集脚本不会自动卸载"; then
          ssh_login_command delete --name "$name"
        fi
        ;;
      *) printf '无效选择。\n' ;;
    esac
  done
}

configure_telegram() {
  local action token ids
  while true; do
    printf '\nTelegram Bot：分组上线通知与在线流量查询\n'
    telegram_command show || true
    printf '\n1. 配置或更新 Bot\n2. 删除 Bot 配置\n0. 返回\n'
    read -rp '请选择 [0-2]: ' action
    case "$action" in
      0) return 0 ;;
      1)
        read -rp 'Bot Token（输入可见）: ' token
        read -rp '允许访问的 Telegram 用户 ID（逗号分隔）: ' ids
        [ -n "$token" ] && [ -n "$ids" ] || { printf 'Token 和用户 ID 都不能为空。\n'; continue; }
        if printf '%s' "$token" | telegram_command set --user-ids "$ids"; then
          unset token
          compose up -d xui-exporter
          compose restart xui-exporter
          printf 'Telegram Bot 配置已生效。\n'
        else
          unset token
          printf '配置失败，请检查 Token 和用户 ID。\n'
        fi
        ;;
      2)
        if telegram_command delete; then
          compose restart xui-exporter
          printf 'Telegram Bot 配置已删除。\n'
        fi
        ;;
      *) printf '无效选择。\n' ;;
    esac
  done
}

select_api_panel() {
  local rows choice i=0
  local -a names=() urls=() logs=() intervals=() counts=()
  rows="$(panels_command list --format tsv)" || return 1
  if [ -z "$rows" ]; then
    printf '尚未配置面板，请先添加。\n'
    return 1
  fi
  while IFS=$'\t' read -r SELECTED_NAME SELECTED_URL SELECTED_LOGS SELECTED_INTERVAL SELECTED_COUNT; do
    names+=("$SELECTED_NAME"); urls+=("$SELECTED_URL"); logs+=("$SELECTED_LOGS")
    intervals+=("$SELECTED_INTERVAL"); counts+=("$SELECTED_COUNT")
    i=$((i + 1))
    printf '%s. %s  %s\n' "$i" "$SELECTED_NAME" "$SELECTED_URL"
  done <<<"$rows"
  while true; do
    read -rp "选择面板编号 [1-$i]（0 返回）: " choice
    case "$choice" in 0|"") return 1 ;; esac
    if [[ "$choice" =~ ^[1-9][0-9]*$ ]] && [ "${#choice}" -le "${#i}" ] && [ "$choice" -le "$i" ]; then
      choice=$((choice - 1))
      SELECTED_NAME="${names[$choice]}"; SELECTED_URL="${urls[$choice]}"
      SELECTED_LOGS="${logs[$choice]}"; SELECTED_INTERVAL="${intervals[$choice]}"
      SELECTED_COUNT="${counts[$choice]}"
      return 0
    fi
    printf '无效编号，请重新选择。\n'
  done
}

edit_api_panel() {
  local operation="$1" name url token logs interval log_count current_logs
  local -a args
  if [ "$operation" = update ]; then
    select_api_panel || return 1
    args=(update --name "$SELECTED_NAME")
    printf '\n修改面板：%s；直接回车保留对应配置。\n' "$SELECTED_NAME"
    read -rp "服务器名称 [$SELECTED_NAME]: " name
    [ -z "$name" ] || args+=(--new-name "$name")
    read -rp "完整 HTTPS API 地址 [$SELECTED_URL]: " url
    [ -z "$url" ] || args+=(--url "$url")
    current_logs="$SELECTED_LOGS"
    interval="$SELECTED_INTERVAL"
    log_count="$SELECTED_COUNT"
  else
    read -rp '服务器名称（唯一，字母/数字/点/下划线/连字符）: ' name
    read -rp '完整 HTTPS API 地址（以 /panel/api/inbounds/list 结尾）: ' url
    args=(add --name "$name" --url "$url")
    current_logs=true; interval=30; log_count=1000
  fi
  printf '采集访问日志需要在对应 3x-ui 中开启 access log。\n'
  if [ "$current_logs" = true ]; then current_logs=开启; else current_logs=关闭; fi
  while true; do
    read -rp "采集访问日志？[当前：$current_logs；y 开启 / n 关闭；回车保留]: " logs
    case "$logs" in
      "") break ;;
      y|Y) args+=(--logs true); break ;;
      n|N) args+=(--logs false); break ;;
      *) printf '请输入 y 或 n，或直接回车。\n' ;;
    esac
  done
  read -rp "采集间隔（10-3600 秒）[$interval]: " interval
  [ -z "$interval" ] || args+=(--interval "$interval")
  read -rp "每次获取日志条数（100-10000）[$log_count]: " log_count
  [ -z "$log_count" ] || args+=(--log-count "$log_count")
  if [ "$operation" = update ]; then
    read -rp 'API Token（输入可见；回车保留已保存的 Token）: ' token
  else
    read -rp 'API Token（输入可见；新面板必须填写）: ' token
  fi
  if ! printf '%s' "$token" | panels_command "${args[@]}"; then
    unset token
    printf '配置失败：请检查名称是否重复、地址、间隔和 Token。\n'
    return 1
  fi
  unset token
  compose up -d xui-exporter
  compose restart xui-exporter
  printf '配置已生效。API 和日志采集状态请在 Grafana 中查看。\n'
}

configure_api_panels() {
  local action
  while true; do
    printf '\n3x-ui API 中心采集：流量、在线状态与访问日志\n'
    panels_command list || return 1
    printf '\n1. 添加面板\n2. 修改面板\n3. 删除面板\n0. 返回\n'
    read -rp '请选择 [0-3]: ' action
    case "$action" in
      0) return 0 ;;
      1) edit_api_panel add || continue ;;
      2) edit_api_panel update || continue ;;
      3)
        select_api_panel || continue
        if panels_command delete --name "$SELECTED_NAME"; then
          compose restart xui-exporter
          printf '已移除采集配置，历史数据保留。\n'
        fi
        ;;
      *) printf '无效选择。\n' ;;
    esac
  done
}

update_stack() {
  printf '正在拉取 Grafana、Loki、VictoriaMetrics 和 Alloy 的最新镜像，服务可能短暂重建。\n'
  compose pull
  compose up -d
  printf '服务组件更新完成。\n'
}

uninstall_everything() {
  if ! confirm "永久删除观测平台、历史日志、指标、Grafana 数据和项目镜像？"; then
    printf '已取消。\n'
    return
  fi
  [ -f "$COMPOSE_FILE" ] && compose down -v || true
  docker volume ls -q --filter label=com.docker.compose.project=xray-log-dashboard | xargs -r docker volume rm
  docker image rm grafana/grafana:latest grafana/loki:latest grafana/alloy:latest victoriametrics/victoria-metrics:latest python:3.12-alpine 2>/dev/null || true
  rm -rf "$STACK_DIR"
  rm -f "$0"
  printf 'GLA 已完整卸载。NPM 和外部 Docker 网络未被修改。\n'
  exit 0
}

while true; do
  clear
  show_header
  cat <<'MENU'

0. 退出
1. 更新配置、脚本与仪表盘
2. 管理 3x-ui API 面板（本机与远程的流量、访问日志）
3. 管理 SSH 登录告警（多服务器与可信 IP）
4. 管理 Telegram Bot（分组通知与在线查询）
5. 服务控制
6. 查看运行状态与资源占用
7. 查看服务日志
8. 查看访问地址、凭据与模块
9. 更新容器镜像
10. 卸载并删除全部数据
MENU
  read -rp "请输入操作编号 [0-10]: " choice
  case "$choice" in
    0) exit 0 ;;
    1) update_script_and_deploy; exit $? ;;
    2) configure_api_panels; pause ;;
    3) configure_ssh_login; pause ;;
    4) configure_telegram; pause ;;
    5) service_control_menu ;;
    6) show_status; pause ;;
    7) show_logs; pause ;;
    8) show_access_info; pause ;;
    9) update_stack; pause ;;
    10) uninstall_everything ;;
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
GLA 3.0.0 - 轻量服务器观测平台

当前状态：[尚未安装或需要重新部署]

0. 退出
1. 安装或更新中心观测平台
MENU
    read -rp "请输入操作编号 [0-1]: " choice
    case "$choice" in
      0) exit 0 ;;
      1) install_stack; return ;;
      *) printf '无效选择。\n'; read -rp "按 Enter 键继续..." _ ;;
    esac
  done
}

case "${GLA_ACTION:-menu}" in
  install) install_stack ;;
  menu) show_installer_menu ;;
  *) die "未知操作：${GLA_ACTION}" ;;
esac
