#!/usr/bin/env bash
# Deploys a small Grafana + Loki + Alloy stack for Xray access logs.
set -Eeuo pipefail

STACK_DIR="${STACK_DIR:-/opt/xray-log-dashboard}"
XRAY_LOG="${XRAY_LOG:-/var/log/x-ui/access.log}"
GRAFANA_BIND="${GRAFANA_BIND:-0.0.0.0}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
LOKI_RETENTION="${LOKI_RETENTION:-168h}"

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }

[ "$(id -u)" -eq 0 ] || die "Run this script as root: sudo bash $0"
[ -r "$XRAY_LOG" ] || die "Xray log not readable: $XRAY_LOG"
command -v docker >/dev/null 2>&1 || die "Docker is required. Nginx Proxy Manager normally already installs it."
docker info >/dev/null 2>&1 || die "Docker daemon is not running or is not accessible."

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose is required (docker compose or docker-compose)."
fi

mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
if [ "$mem_kb" -lt 1258291 ]; then
  printf 'Warning: this server has less than 1.2 GB RAM. Grafana may be slow; keep swap enabled and stop unused services.\n' >&2
fi

note "Creating deployment files in $STACK_DIR"
install -d -m 0750 "$STACK_DIR" "$STACK_DIR/alloy" \
  "$STACK_DIR/loki" "$STACK_DIR/grafana/provisioning/datasources" \
  "$STACK_DIR/grafana/provisioning/dashboards" "$STACK_DIR/grafana/dashboards"

# Grafana only applies its admin password on the first database initialization.
# Preserve the generated value so a later script run cannot print a false password.
CREDENTIALS_FILE="$STACK_DIR/.credentials"
if [ -f "$CREDENTIALS_FILE" ]; then
  GRAFANA_ADMIN_PASSWORD="$(awk -F= '$1 == "GRAFANA_ADMIN_PASSWORD" {print substr($0, index($0, "=") + 1); exit}' "$CREDENTIALS_FILE")"
  [ -n "$GRAFANA_ADMIN_PASSWORD" ] || die "Invalid credentials file: $CREDENTIALS_FILE"
elif [ -z "${GRAFANA_ADMIN_PASSWORD:-}" ]; then
  GRAFANA_ADMIN_PASSWORD="$(od -An -N16 -tx1 /dev/urandom | tr -d ' \\n')"
fi
printf 'GRAFANA_ADMIN_PASSWORD=%s\\n' "$GRAFANA_ADMIN_PASSWORD" >"$CREDENTIALS_FILE"
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
    volumes:
      - ./loki/config.yaml:/etc/loki/config.yaml:ro
      - loki-data:/loki

  alloy:
    image: grafana/alloy:latest
    container_name: xray-alloy
    restart: unless-stopped
    user: "0:0"
    command: run --server.http.listen-addr=0.0.0.0:12345 --storage.path=/var/lib/alloy/data /etc/alloy/config.alloy
    volumes:
      - ${XRAY_LOG}:${XRAY_LOG}:ro
      - ./alloy/config.alloy:/etc/alloy/config.alloy:ro
      - alloy-data:/var/lib/alloy/data
    depends_on:
      - loki

volumes:
  grafana-data:
  loki-data:
  alloy-data:
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
  }]
}

loki.source.file "xray_access" {
  targets    = local.file_match.xray_access.targets
  forward_to = [loki.process.xray_access.receiver]
}

loki.process "xray_access" {
  forward_to = [loki.write.default.receiver]

  stage.regex {
    expression = "^(?P<timestamp>\\\\d{4}/\\\\d{2}/\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2}\\\\.\\\\d+) from (?P<source>\\\\S+) accepted (?P<network>\\\\w+):(?P<destination>\\\\S+) \\\\[(?P<inbound>[^ ]+) >> (?P<outbound>[^\\\\]]+)\\\\](?: email: (?P<email>\\\\S+))?"
  }

  stage.labels {
    values = {
      inbound  = "",
      outbound = "",
      email    = "",
    }
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

cat >"$STACK_DIR/grafana/dashboards/xray-access.json" <<'EOF'
{
  "uid": "xray-access",
  "title": "Xray 访问日志",
  "timezone": "browser",
  "schemaVersion": 39,
  "version": 4,
  "refresh": "30s",
  "time": { "from": "now-1h", "to": "now" },
  "templating": {
    "list": [
      {
        "name": "client",
        "label": "客户端",
        "type": "query",
        "datasource": { "type": "loki", "uid": "loki" },
        "definition": "label_values({job=\"xray-access\", email=~\".+\"}, email)",
        "query": "label_values({job=\"xray-access\", email=~\".+\"}, email)",
        "refresh": 1,
        "includeAll": true,
        "allValue": ".*",
        "multi": false,
        "options": [],
        "current": { "selected": true, "text": "全部", "value": "$__all" }
      },
      {
        "name": "site",
        "label": "网站关键词",
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
      "targets": [{ "refId": "A", "expr": "{job=\"xray-access\"}" }],
      "gridPos": { "x": 0, "y": 0, "w": 24, "h": 9 },
      "options": { "dedupStrategy": "none", "enableLogDetails": true, "showCommonLabels": false, "wrapLogMessage": true, "sortOrder": "Descending" }
    },
    {
      "id": 2,
      "type": "logs",
      "title": "真实访问日志（已排除 API）",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "{job=\"xray-access\"} != \"[api -> api]\"" }],
      "gridPos": { "x": 0, "y": 9, "w": 24, "h": 9 },
      "options": { "dedupStrategy": "none", "enableLogDetails": true, "showCommonLabels": false, "wrapLogMessage": true, "sortOrder": "Descending" }
    },
    {
      "id": 3,
      "type": "logs",
      "title": "所选客户端的网站访问记录",
      "description": "选择客户端后，可在顶部“网站关键词”输入域名或 IP；留空则显示该客户端全部访问记录。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "{job=\"xray-access\", email=~\"$client\"} != \"[api -> api]\" |= \"$site\"" }],
      "gridPos": { "x": 0, "y": 18, "w": 24, "h": 9 },
      "options": { "dedupStrategy": "none", "enableLogDetails": true, "showCommonLabels": false, "wrapLogMessage": true, "sortOrder": "Descending" }
    },
    {
      "id": 4,
      "type": "stat",
      "title": "匹配网站的连接次数",
      "description": "统计所选客户端和网站关键词在当前时间范围内匹配到的 Xray 连接记录数。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "sum(count_over_time({job=\"xray-access\", email=~\"$client\"} != \"[api -> api]\" |= \"$site\" [$__range]))", "instant": true }],
      "gridPos": { "x": 0, "y": 27, "w": 6, "h": 5 },
      "options": { "reduceOptions": { "values": false, "calcs": ["lastNotNull"], "fields": "" }, "orientation": "auto", "textMode": "auto", "colorMode": "value", "graphMode": "none", "justifyMode": "auto" }
    },
    {
      "id": 5,
      "type": "table",
      "title": "所选客户端近 $period 访问目标 Top 10",
      "description": "按目标域名或 IP 聚合。选择“客户端”和“统计周期”后自动更新。",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "topk(10, sum by (destination) (count_over_time({job=\"xray-access\", email=~\"$client\"} != \"[api -> api]\" | pattern `<_> accepted <_>:<destination>:<port> [<_>` [$period])))", "instant": true, "format": "table" }],
      "gridPos": { "x": 0, "y": 32, "w": 24, "h": 9 },
      "options": { "showHeader": true }
    },
    {
      "id": 6,
      "type": "table",
      "title": "按客户端连接数",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "sum by (email) (count_over_time({job=\"xray-access\"}[$__range]))", "instant": true, "format": "table" }],
      "gridPos": { "x": 0, "y": 41, "w": 12, "h": 8 },
      "options": { "showHeader": true }
    },
    {
      "id": 7,
      "type": "table",
      "title": "按入站连接数",
      "datasource": { "type": "loki", "uid": "loki" },
      "targets": [{ "refId": "A", "expr": "sum by (inbound) (count_over_time({job=\"xray-access\"}[$__range]))", "instant": true, "format": "table" }],
      "gridPos": { "x": 12, "y": 41, "w": 12, "h": 8 },
      "options": { "showHeader": true }
    }
  ]
}
EOF

chmod 0600 "$STACK_DIR/compose.yaml"

note "Starting Grafana, Loki, and Alloy"
cd "$STACK_DIR"
"${COMPOSE[@]}" pull
"${COMPOSE[@]}" up -d

note "Verifying containers"
"${COMPOSE[@]}" ps

cat <<EOF

Deployment complete.

Grafana URL: http://SERVER_IP:${GRAFANA_PORT}
Grafana user: admin
Grafana password: ${GRAFANA_ADMIN_PASSWORD}

Security: port ${GRAFANA_PORT} is exposed on all server network interfaces.
Restrict it to your management IP in the cloud firewall or server firewall before using it publicly.

Management commands:
  cd ${STACK_DIR} && ${COMPOSE[*]} ps
  cd ${STACK_DIR} && ${COMPOSE[*]} logs -f alloy
  cd ${STACK_DIR} && ${COMPOSE[*]} down
EOF
