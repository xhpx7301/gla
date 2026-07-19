#!/usr/bin/env bash
# Deploys a small Grafana + Loki + Alloy stack for Xray access logs.
set -Eeuo pipefail

STACK_DIR="${STACK_DIR:-/opt/xray-log-dashboard}"
XRAY_LOG="${XRAY_LOG:-/var/log/x-ui/access.log}"
GRAFANA_BIND="${GRAFANA_BIND:-0.0.0.0}"
GRAFANA_PORT="${GRAFANA_PORT:-3000}"
LOKI_RETENTION="${LOKI_RETENTION:-168h}"
SERVER_NAME="${SERVER_NAME:-central}"
NPM_NETWORK="${NPM_NETWORK:-npm-loki}"
MANAGER_PATH="/usr/local/bin/gla"
INSTALL_SETTINGS_FILE="$STACK_DIR/.install.env"

die() { printf '错误: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }

write_install_settings() {
  {
    printf 'STACK_DIR=%q\n' "$STACK_DIR"
    printf 'XRAY_LOG=%q\n' "$XRAY_LOG"
    printf 'GRAFANA_BIND=%q\n' "$GRAFANA_BIND"
    printf 'GRAFANA_PORT=%q\n' "$GRAFANA_PORT"
    printf 'LOKI_RETENTION=%q\n' "$LOKI_RETENTION"
    printf 'SERVER_NAME=%q\n' "$SERVER_NAME"
    printf 'NPM_NETWORK=%q\n' "$NPM_NETWORK"
  } >"$INSTALL_SETTINGS_FILE"
  chmod 0600 "$INSTALL_SETTINGS_FILE"
}

require_install_prerequisites() {
  [ "$(id -u)" -eq 0 ] || die "请使用 root 执行：sudo bash $0"
  [ -r "$XRAY_LOG" ] || die "无法读取 Xray 日志：$XRAY_LOG"
  [[ "$SERVER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die "SERVER_NAME 只能包含字母、数字、点、下划线和连字符。"
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

install_stack() {
  require_install_prerequisites
  docker network inspect "$NPM_NETWORK" >/dev/null 2>&1 || docker network create "$NPM_NETWORK" >/dev/null

  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo)"
  if [ "$mem_kb" -lt 1258291 ]; then
    printf '警告：此服务器内存低于 1.2 GB，Grafana 可能较慢。请保留 Swap，并停止不需要的服务。\n' >&2
  fi

note "正在创建部署文件：$STACK_DIR"
install -d -m 0750 "$STACK_DIR" "$STACK_DIR/alloy" \
  "$STACK_DIR/loki" "$STACK_DIR/grafana/provisioning/datasources" \
  "$STACK_DIR/grafana/provisioning/dashboards" "$STACK_DIR/grafana/dashboards"
write_install_settings

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
    expression = "^(?P<timestamp>\\\\d{4}/\\\\d{2}/\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2}\\\\.\\\\d+) from (?P<source>\\\\S+) accepted (?P<network>\\\\w+):(?P<destination>\\\\S+) \\\\[(?P<inbound>[^ ]+) (?:>>|->) (?P<outbound>[^\\\\]]+)\\\\](?: email: (?P<email>\\\\S+))?"
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

note "正在启动 Grafana、Loki 和 Alloy"
cd "$STACK_DIR"
"${COMPOSE[@]}" pull
"${COMPOSE[@]}" up -d

note "正在验证容器状态"
"${COMPOSE[@]}" ps

install_manager

cat <<EOF

部署完成。

Grafana 地址：http://服务器_IP:${GRAFANA_PORT}
Grafana 用户名：admin
Grafana 密码：${GRAFANA_ADMIN_PASSWORD}

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
  for image in grafana/grafana:latest grafana/loki:latest grafana/alloy:latest; do
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
  printf '\n1. Grafana\n2. Loki\n3. Alloy\n0. 返回\n'
  read -rp "请选择服务: " choice
  case "$choice" in
    1) compose logs -f --tail=100 grafana ;;
    2) compose logs -f --tail=100 loki ;;
    3) compose logs -f --tail=100 alloy ;;
    0) return ;;
    *) printf '无效选择。\n' ;;
  esac
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
  [ -r "$INSTALL_SETTINGS_FILE" ] || die "未找到安装配置：$INSTALL_SETTINGS_FILE"
  printf '正在从 GitHub 下载最新版脚本并重新部署，服务可能短暂重建。\n'
  set -a
  # This file is created locally by the installer and contains no password.
  . "$INSTALL_SETTINGS_FILE"
  set +a
  GLA_ACTION=install bash <(download_installer)
}

update_stack() {
  printf '正在拉取 Grafana、Loki 和 Alloy 的最新镜像，服务可能短暂重建。\n'
  compose pull
  compose up -d
  printf '服务组件更新完成。\n'
}

uninstall_everything() {
  if ! confirm "永久删除此日志面板、Loki 历史日志、Grafana 数据和项目镜像？"; then
    printf '已取消。\n'
    return
  fi
  [ -f "$COMPOSE_FILE" ] && compose down -v || true
  docker volume ls -q --filter label=com.docker.compose.project=xray-log-dashboard | xargs -r docker volume rm
  docker image rm grafana/grafana:latest grafana/loki:latest grafana/alloy:latest 2>/dev/null || true
  rm -rf "$STACK_DIR"
  rm -f "$0"
  printf 'Xray 日志面板已完整卸载。NPM 和 npm-loki 网络未被修改。\n'
  exit 0
}

while true; do
  clear
  cat <<'MENU'
Xray 访问日志面板管理

0. 退出
1. 安装或更新脚本并重新部署
2. 启动服务
3. 停止服务
4. 重启服务
5. 查看服务状态与磁盘占用
6. 查看服务日志
7. 查看 Grafana 密码
8. 更新 Grafana、Loki 和 Alloy
9. 卸载并删除所有面板数据
MENU
  read -rp "请输入操作编号 [0-9]: " choice
  case "$choice" in
    0) exit 0 ;;
    1) update_script_and_deploy; exit $? ;;
    2) compose up -d; pause ;;
    3) compose stop; pause ;;
    4) compose restart; pause ;;
    5) show_status; pause ;;
    6) show_logs; pause ;;
    7) [ -f "$STACK_DIR/.credentials" ] && sed -n 's/^GRAFANA_ADMIN_PASSWORD=/Grafana 密码：/p' "$STACK_DIR/.credentials" || printf '未找到凭据文件。\n'; pause ;;
    8) update_stack; pause ;;
    9) uninstall_everything ;;
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
Xray 访问日志面板管理

0. 退出
1. 安装或更新日志面板
2. 启动服务
3. 停止服务
4. 重启服务
5. 查看服务状态与磁盘占用
6. 查看服务日志
7. 查看 Grafana 密码
8. 更新 Grafana、Loki 和 Alloy
9. 卸载并删除所有面板数据
MENU
    read -rp "请输入操作编号 [0-9]: " choice
    case "$choice" in
      0) exit 0 ;;
      1) install_stack; return ;;
      *) printf '请先选择 1 安装或更新日志面板；安装完成后输入 gla 使用完整管理功能。\n'; read -rp "按 Enter 键继续..." _ ;;
    esac
  done
}

case "${GLA_ACTION:-menu}" in
  install) install_stack ;;
  menu) show_installer_menu ;;
  *) die "未知操作：${GLA_ACTION}" ;;
esac
