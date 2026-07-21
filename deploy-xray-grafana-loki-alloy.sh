#!/usr/bin/env bash
# Deploys the central GLA observability stack.
set -Eeuo pipefail

STACK_DIR="${STACK_DIR:-/opt/xray-log-dashboard}"
XRAY_LOG="${XRAY_LOG:-/var/log/x-ui/access.log}"
ENABLE_XRAY="${ENABLE_XRAY:-auto}"
ENABLE_GEOIP="${ENABLE_GEOIP:-auto}"
GRAFANA_BIND="${GRAFANA_BIND:-0.0.0.0}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
LOKI_RETENTION="${LOKI_RETENTION:-168h}"
METRICS_RETENTION="${METRICS_RETENTION:-14d}"
SERVER_NAME="${SERVER_NAME:-central}"
NPM_NETWORK="${NPM_NETWORK:-npm-loki}"
ASSET_BASE_URL="${ASSET_BASE_URL:-https://raw.githubusercontent.com/xhpx7301/gla/main}"
XUI_API_URL="${XUI_API_URL:-}"
XUI_API_TOKEN="${XUI_API_TOKEN:-}"
MANAGER_PATH="/usr/local/bin/gla"
INSTALL_SETTINGS_FILE="$STACK_DIR/.install.env"
GEOIP_DB_PATH="${GEOIP_DB_PATH:-$STACK_DIR/geoip/GeoLite2-City.mmdb}"

die() { printf '错误: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }

write_install_settings() {
  {
    printf 'STACK_DIR=%q\n' "$STACK_DIR"
    printf 'XRAY_LOG=%q\n' "$XRAY_LOG"
    printf 'ENABLE_XRAY=%q\n' "$ENABLE_XRAY"
    printf 'ENABLE_GEOIP=%q\n' "$ENABLE_GEOIP"
    printf 'GEOIP_DB_PATH=%q\n' "$GEOIP_DB_PATH"
    printf 'GRAFANA_BIND=%q\n' "$GRAFANA_BIND"
    printf 'GRAFANA_PORT=%q\n' "$GRAFANA_PORT"
    printf 'LOKI_RETENTION=%q\n' "$LOKI_RETENTION"
    printf 'METRICS_RETENTION=%q\n' "$METRICS_RETENTION"
    printf 'SERVER_NAME=%q\n' "$SERVER_NAME"
    printf 'NPM_NETWORK=%q\n' "$NPM_NETWORK"
    printf 'XUI_API_URL=%q\n' "$XUI_API_URL"
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
  case "$ENABLE_GEOIP" in
    auto) [ -r "$GEOIP_DB_PATH" ] && ENABLE_GEOIP=true || ENABLE_GEOIP=false ;;
    true) [ -r "$GEOIP_DB_PATH" ] || die "已要求启用 GeoIP，但无法读取数据库：$GEOIP_DB_PATH" ;;
    false) ;;
    *) die "ENABLE_GEOIP 只能是 auto、true 或 false。" ;;
  esac
  [[ "$SERVER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die "SERVER_NAME 只能包含字母、数字、点、下划线和连字符。"
  if [ -n "$XUI_API_URL" ]; then
    [[ "$XUI_API_URL" =~ ^https://.+/panel/api/inbounds/list$ ]] || die "XUI_API_URL 必须是 HTTPS 且以 /panel/api/inbounds/list 结尾。"
    case "$XUI_API_URL" in *$'\n'*|*$'\r'*|*\"*) die "XUI_API_URL 包含不支持的字符。" ;; esac
    XUI_API_HOST="${XUI_API_URL#https://}"
    XUI_API_HOST="${XUI_API_HOST%%/*}"
    XUI_API_HOST="${XUI_API_HOST%%:*}"
    [[ "$XUI_API_HOST" =~ ^[A-Za-z0-9][A-Za-z0-9.-]*$ ]] || die "无法从 XUI_API_URL 识别面板域名。"
  else
    XUI_API_HOST="localhost"
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

install_stack() {
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
      read -rsp "请输入 3x-ui API Token（输入内容不会显示）: " XUI_API_TOKEN
      printf '\n'
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
write_install_settings

printf '%s\n' "$XUI_API_TOKEN" >"$STACK_DIR/secrets/xui-api-token"
chmod 0600 "$STACK_DIR/secrets/xui-api-token"
download_asset assets/xui_exporter.py "$STACK_DIR/assets/xui_exporter.py"
if [ -n "$XUI_API_URL" ]; then
  printf 'COMPOSE_PROFILES=xui\n' >"$STACK_DIR/.env"
else
  printf 'COMPOSE_PROFILES=\n' >"$STACK_DIR/.env"
fi
chmod 0600 "$STACK_DIR/.env"

XUI_SCRAPE_CONFIG=""
if [ -n "$XUI_API_URL" ]; then
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
fi

XRAY_VOLUME_LINE=""
if [ "$ENABLE_XRAY" = true ]; then
  XRAY_VOLUME_LINE="      - ${XRAY_LOG}:${XRAY_LOG}:ro"
fi

GEOIP_VOLUME_LINE=""
if [ "$ENABLE_GEOIP" = true ]; then
  GEOIP_VOLUME_LINE="      - ${GEOIP_DB_PATH}:/var/lib/gla/geoip/GeoLite2-City.mmdb:ro"
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
  }

  stage.labels {
    values = {
      geo_country = "geoip_country_name",
      geo_region  = "geoip_subdivision_name",
      geo_city    = "geoip_city_name",
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
${XRAY_VOLUME_LINE}
${GEOIP_VOLUME_LINE}
      - ${JOURNAL_PATH_HOST}:/var/log/journal:ro
      - /etc/machine-id:/etc/machine-id:ro
      - /var/log:/host/var/log:ro
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/host/root:ro,rslave
      - ./alloy/config.alloy:/etc/alloy/config.alloy:ro
      - alloy-data:/var/lib/alloy/data
    depends_on:
      - loki
      - victoriametrics

  xui-exporter:
    profiles: ["xui"]
    image: python:3.12-alpine
    container_name: gla-xui-exporter
    restart: unless-stopped
    command: ["python3", "/app/xui_exporter.py"]
    environment:
      XUI_API_URL: "${XUI_API_URL}"
      XUI_API_TOKEN_FILE: /run/secrets/xui_api_token
      SERVER_NAME: "${SERVER_NAME}"
    extra_hosts:
      - "${XUI_API_HOST}:host-gateway"
    volumes:
      - ./assets/xui_exporter.py:/app/xui_exporter.py:ro
      - ./secrets/xui-api-token:/run/secrets/xui_api_token:ro

volumes:
  grafana-data:
  loki-data:
  victoria-metrics-data:
  alloy-data:

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
local.file_match "xray_access" {
  path_targets = [{
    __path__ = "${XRAY_LOG}",
    job      = "xray-access",
    server   = "${SERVER_NAME}",
  }]
}

loki.source.file "xray_access" {
  targets    = local.file_match.xray_access.targets
  forward_to = [loki.process.xray_access.receiver]
}

loki.process "xray_access" {
  forward_to = [loki.write.default.receiver]

  stage.regex {
    expression = "^(?P<timestamp>\\\\d{4}/\\\\d{2}/\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2}\\\\.\\\\d+) from (?P<source_ip>(?:\\\\[[0-9A-Fa-f:]+\\\\]|[0-9A-Fa-f:.]+)):[0-9]+ accepted (?P<network>\\\\w+):(?P<destination>\\\\S+) \\\\[(?P<inbound>[^ ]+) (?:>>|->) (?P<outbound>[^\\\\]]+)\\\\](?: email: (?P<email>\\\\S+))?"
  }

${GEOIP_XRAY_STAGES}

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

cat >"$STACK_DIR/grafana/dashboards/xray-access.json" <<'EOF'
{
  "uid": "xray-access",
  "title": "Xray 访问日志",
  "timezone": "browser",
  "schemaVersion": 39,
  "version": 11,
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
      {
        "name": "client",
        "label": "客户端",
        "type": "query",
        "datasource": { "type": "loki", "uid": "loki" },
        "definition": "label_values({job=\"xray-access\", server=~\"$server\", email=~\".+\"}, email)",
        "query": "label_values({job=\"xray-access\", server=~\"$server\", email=~\".+\"}, email)",
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
      "title": "全部访问日志（含 API）",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "{job=\"xray-access\", server=~\"$server\"}" }],
      "gridPos": { "x": 0, "y": 35, "w": 24, "h": 9 },
      "options": { "dedupStrategy": "none", "enableLogDetails": true, "showCommonLabels": false, "wrapLogMessage": true, "sortOrder": "Descending" }
    },
    {
      "id": 2,
      "type": "logs",
      "title": "真实访问日志（已排除 API）",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "{job=\"xray-access\", server=~\"$server\"} != \"[api -> api]\"" }],
      "gridPos": { "x": 0, "y": 26, "w": 24, "h": 9 },
      "options": { "dedupStrategy": "none", "enableLogDetails": true, "showCommonLabels": false, "wrapLogMessage": true, "sortOrder": "Descending" }
    },
    {
      "id": 3,
      "type": "logs",
      "title": "所选客户端的访问记录",
      "description": "选择客户端后，可在顶部“访问目标关键词”输入域名或 IP；留空则显示该客户端全部访问记录。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "{job=\"xray-access\", server=~\"$server\", email=~\"$client\"} != \"[api -> api]\" |= \"$site\"" }],
      "gridPos": { "x": 0, "y": 0, "w": 20, "h": 9 },
      "options": { "dedupStrategy": "none", "enableLogDetails": true, "showCommonLabels": false, "wrapLogMessage": true, "sortOrder": "Descending" }
    },
    {
      "id": 4,
      "type": "stat",
      "title": "匹配访问目标的连接次数",
      "description": "统计所选客户端和访问目标关键词在当前时间范围内匹配到的 Xray 连接记录数。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "sum(count_over_time({job=\"xray-access\", server=~\"$server\", email=~\"$client\"} != \"[api -> api]\" |= \"$site\" [$__range]))", "instant": true }],
      "gridPos": { "x": 20, "y": 0, "w": 4, "h": 4 },
      "options": { "reduceOptions": { "values": false, "calcs": ["lastNotNull"], "fields": "" }, "orientation": "auto", "textMode": "auto", "colorMode": "value", "graphMode": "none", "justifyMode": "auto" }
    },
    {
      "id": 5,
      "type": "table",
      "title": "所选客户端近 $period 访问目标 Top 10",
      "description": "按目标域名或 IP 聚合。选择“客户端”和“统计周期”后自动更新。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "topk(10, sum by (destination) (count_over_time({job=\"xray-access\", server=~\"$server\", email=~\"$client\"} != \"[api -> api]\" | pattern `<_> accepted <_>:<destination>:<port> [<_>` [$period])))", "instant": true, "format": "table" }],
      "gridPos": { "x": 0, "y": 9, "w": 24, "h": 9 },
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
            "excludeByName": {},
            "indexByName": { "Time": 0, "destination": 1, "Value #A": 2 },
            "renameByName": { "Time": "时间", "destination": "访问目标", "Value #A": "次数" }
          }
        }
      ]
    },
    {
      "id": 6,
      "type": "table",
      "title": "按客户端连接数",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "sum by (email) (count_over_time({job=\"xray-access\", server=~\"$server\"}[$__range]))", "instant": true, "format": "table" }],
      "gridPos": { "x": 0, "y": 18, "w": 12, "h": 8 },
      "options": { "showHeader": true },
      "transformations": [{
        "id": "organize",
        "options": {
          "excludeByName": {},
          "indexByName": { "Time": 0, "email": 1, "Value #A": 2 },
          "renameByName": { "Time": "时间", "email": "客户端", "Value #A": "次数" }
        }
      }]
    },
    {
      "id": 7,
      "type": "table",
      "title": "按入站连接数",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "sum by (inbound) (count_over_time({job=\"xray-access\", server=~\"$server\"}[$__range]))", "instant": true, "format": "table" }],
      "gridPos": { "x": 12, "y": 18, "w": 12, "h": 8 },
      "options": { "showHeader": true },
      "transformations": [{
        "id": "organize",
        "options": {
          "excludeByName": {},
          "indexByName": { "Time": 0, "inbound": 1, "Value #A": 2 },
          "renameByName": { "Time": "时间", "inbound": "入站", "Value #A": "次数" }
        }
      }]
    }
  ]
}
EOF

chmod 0600 "$STACK_DIR/compose.yaml"

note "正在启动 Grafana、Loki、VictoriaMetrics 和 Alloy"
cd "$STACK_DIR"
"${COMPOSE[@]}" pull
"${COMPOSE[@]}" up -d
if [ -n "$XUI_API_URL" ]; then
  "${COMPOSE[@]}" restart xui-exporter
else
  docker rm -f gla-xui-exporter >/dev/null 2>&1 || true
fi
"${COMPOSE[@]}" restart alloy

note "正在验证容器状态"
"${COMPOSE[@]}" ps

install_manager

cat <<EOF

部署完成。

Grafana 地址：http://服务器_IP:${GRAFANA_PORT}
Grafana 用户名：admin
Grafana 密码：${GRAFANA_ADMIN_PASSWORD}
Xray 日志采集：${ENABLE_XRAY}

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

GLA_VERSION="2.1.0"
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

xui_state() {
  if ! grep -Eq '^COMPOSE_PROFILES=.*xui' "$STACK_DIR/.env" 2>/dev/null; then
    printf '[未启用]'
  else
    container_state gla-xui-exporter
  fi
}

show_header() {
  printf 'GLA %s - 轻量服务器观测平台\n\n' "$GLA_VERSION"
  printf 'Grafana         %s    Loki             %s\n' "$(container_state xray-grafana)" "$(container_state xray-loki)"
  printf 'VictoriaMetrics %s    Alloy            %s\n' "$(container_state gla-victoriametrics)" "$(container_state xray-alloy)"
  if grep -Eq '^ENABLE_GEOIP=true$' "$STACK_DIR/.install.env" 2>/dev/null; then
    printf 'GeoIP 归属解析   [已启用]\n'
  else
    printf 'GeoIP 归属解析   [未启用]\n'
  fi
  printf '本机 3x-ui 采集 %s\n' "$(xui_state)"
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

  local image volume mountpoint log_path
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

  log_path="$(sed -n 's/^[[:space:]]*__path__[[:space:]]*=[[:space:]]*"\(.*\)",/\1/p' "$STACK_DIR/alloy/config.alloy" 2>/dev/null | head -n 1)"
  if [ -n "$log_path" ] && [ -e "$log_path" ]; then
    printf '\nXray 原始访问日志占用：\n'
    du -sh "$log_path"
  fi
}

show_logs() {
  printf '\n服务日志\n\n1. Grafana\n2. Loki\n3. VictoriaMetrics\n4. Alloy\n5. 本机 3x-ui 流量采集\n0. 返回\n'
  read -rp "请选择服务: " choice
  case "$choice" in
    1) compose logs -f --tail=100 grafana || true ;;
    2) compose logs -f --tail=100 loki || true ;;
    3) compose logs -f --tail=100 victoriametrics || true ;;
    4) compose logs -f --tail=100 alloy || true ;;
    5)
      if grep -Eq '^COMPOSE_PROFILES=.*xui' "$STACK_DIR/.env" 2>/dev/null; then
        compose logs -f --tail=100 xui-exporter || true
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
8. 仅重启本机 3x-ui 流量采集
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
  local grafana_bind="0.0.0.0" grafana_port="3000" xui_api_url="" enable_xray="auto" grafana_password=""
  if [ -r "$INSTALL_SETTINGS_FILE" ]; then
    set +u
    # Generated by GLA and contains shell-escaped non-secret settings.
    . "$INSTALL_SETTINGS_FILE"
    set -u
    grafana_bind="${GRAFANA_BIND:-0.0.0.0}"
    grafana_port="${GRAFANA_PORT:-3000}"
    xui_api_url="${XUI_API_URL:-}"
    enable_xray="${ENABLE_XRAY:-auto}"
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
  [ -f "$STACK_DIR/grafana/dashboards/xray-gateway.json" ] && printf '  - Xray Gateway\n'
  [ -f "$STACK_DIR/grafana/dashboards/server-security.json" ] && printf '  - 服务器安全与系统\n'

  if [ -n "$xui_api_url" ]; then
    printf '\n本机 3x-ui API：已配置（Token 不显示）\n'
  else
    printf '\n本机 3x-ui API：未启用\n'
  fi
  printf 'Xray 日志：    %s\n' "$enable_xray"
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
  local enable_geoip_is_set="${ENABLE_GEOIP+x}" enable_geoip_override="${ENABLE_GEOIP-}"
  local geoip_db_path_is_set="${GEOIP_DB_PATH+x}" geoip_db_path_override="${GEOIP_DB_PATH-}"
  [ -r "$INSTALL_SETTINGS_FILE" ] || die "未找到安装配置：$INSTALL_SETTINGS_FILE"
  printf '正在从 GitHub 下载最新版脚本并重新部署，服务可能短暂重建。\n'
  set -a
  # This file is created locally by the installer and contains no password.
  . "$INSTALL_SETTINGS_FILE"
  set +a
  if [ "$xui_api_url_is_set" = x ]; then
    XUI_API_URL="$xui_api_url_override"
    export XUI_API_URL
  fi
  if [ "$enable_geoip_is_set" = x ]; then
    ENABLE_GEOIP="$enable_geoip_override"
    export ENABLE_GEOIP
  fi
  if [ "$geoip_db_path_is_set" = x ]; then
    GEOIP_DB_PATH="$geoip_db_path_override"
    export GEOIP_DB_PATH
  fi
  GLA_ACTION=install bash <(download_installer)
}

configure_xui_api() {
  local current_url="" answer
  if [ -r "$INSTALL_SETTINGS_FILE" ]; then
    set +u
    . "$INSTALL_SETTINGS_FILE"
    set -u
    current_url="${XUI_API_URL:-}"
  fi

  printf '\n中央服务器 3x-ui API 流量采集配置\n'
  if [ -n "$current_url" ]; then
    printf '当前地址：%s\n' "$current_url"
  else
    printf '当前状态：未启用\n'
  fi
  printf '\n请输入中央服务器本机 3x-ui 的完整 HTTPS API 地址。\n'
  printf '格式：https://面板域名/面板路径/panel/api/inbounds/list\n'
  printf '输入 0 可关闭，直接按 Enter 取消。\n'
  read -rp "3x-ui API 地址: " answer
  if [ -z "$answer" ]; then
    printf '已取消。\n'
    return 1
  fi
  if [ "$answer" = 0 ]; then
    XUI_API_URL=""
  else
    XUI_API_URL="$answer"
  fi
  export XUI_API_URL
  update_script_and_deploy
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
2. 服务控制
3. 查看运行状态与资源占用
4. 查看服务日志
5. 查看访问地址、凭据与模块
6. 更新容器镜像
7. 卸载并删除全部数据
8. 配置或关闭本机 3x-ui API 流量采集
MENU
  read -rp "请输入操作编号 [0-8]: " choice
  case "$choice" in
    0) exit 0 ;;
    1) update_script_and_deploy; exit $? ;;
    2) service_control_menu ;;
    3) show_status; pause ;;
    4) show_logs; pause ;;
    5) show_access_info; pause ;;
    6) update_stack; pause ;;
    7) uninstall_everything ;;
    8)
      if configure_xui_api; then
        exit 0
      else
        pause
      fi
      ;;
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
GLA 2.0.2 - 轻量服务器观测平台

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
