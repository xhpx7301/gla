#!/usr/bin/env bash
# Deploys an Alloy-only Xray access-log collector for a central Loki endpoint.
set -Eeuo pipefail

STACK_DIR="${STACK_DIR:-/opt/xray-alloy-collector}"
XRAY_LOG="${XRAY_LOG:-/var/log/x-ui/access.log}"
SERVER_NAME="${SERVER_NAME:-}"
LOKI_URL="${LOKI_URL:-}"
LOKI_USERNAME="${LOKI_USERNAME:-alloy-agent}"
LOKI_PASSWORD="${LOKI_PASSWORD:-}"
ALLOY_IMAGE="${ALLOY_IMAGE:-grafana/alloy:latest}"
MANAGER_PATH="/usr/local/bin/alloy"
INSTALL_SETTINGS_FILE="$STACK_DIR/.install.env"

die() { printf '错误: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }
hcl_escape() { sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }
compose() { "${COMPOSE[@]}" "$@"; }

write_install_settings() {
  {
    printf 'STACK_DIR=%q\n' "$STACK_DIR"
    printf 'XRAY_LOG=%q\n' "$XRAY_LOG"
    printf 'SERVER_NAME=%q\n' "$SERVER_NAME"
    printf 'LOKI_URL=%q\n' "$LOKI_URL"
    printf 'LOKI_USERNAME=%q\n' "$LOKI_USERNAME"
    printf 'ALLOY_IMAGE=%q\n' "$ALLOY_IMAGE"
  } >"$INSTALL_SETTINGS_FILE"
  chmod 0600 "$INSTALL_SETTINGS_FILE"
}

require_install_prerequisites() {
  [ "$(id -u)" -eq 0 ] || die "请使用 root 执行：sudo bash $0"
  [ -r "$XRAY_LOG" ] || die "无法读取 Xray 日志：$XRAY_LOG"
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
}

install_collector() {
  require_install_prerequisites
  if [ -z "$LOKI_PASSWORD" ]; then
    read -rsp "请输入 Loki Basic Auth 密码: " LOKI_PASSWORD
    printf '\n'
  fi
  [ -n "$LOKI_PASSWORD" ] || die "LOKI_PASSWORD 不能为空。"
  case "$LOKI_PASSWORD" in
    *$'\n'*|*$'\r'*) die "LOKI_PASSWORD 不能包含换行符。" ;;
  esac

  LOKI_URL_HCL="$(printf '%s' "$LOKI_URL" | hcl_escape)"
  LOKI_USERNAME_HCL="$(printf '%s' "$LOKI_USERNAME" | hcl_escape)"
  LOKI_PASSWORD_HCL="$(printf '%s' "$LOKI_PASSWORD" | hcl_escape)"

note "正在创建采集器文件：$STACK_DIR"
install -d -m 0750 "$STACK_DIR/alloy"
write_install_settings

cat >"$STACK_DIR/compose.yaml" <<EOF
services:
  alloy:
    image: $ALLOY_IMAGE
    container_name: xray-alloy
    restart: unless-stopped
    user: "0:0"
    command: run --storage.path=/var/lib/alloy/data /etc/alloy/config.alloy
    volumes:
      - $XRAY_LOG:$XRAY_LOG:ro
      - ./alloy/config.alloy:/etc/alloy/config.alloy:ro
      - alloy-data:/var/lib/alloy/data

volumes:
  alloy-data:
EOF

cat >"$STACK_DIR/alloy/config.alloy" <<EOF
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

chmod 0600 "$STACK_DIR/compose.yaml" "$STACK_DIR/alloy/config.alloy"

note "正在启动 Alloy 采集器"
cd "$STACK_DIR"
compose pull
compose up -d

note "正在验证采集器状态"
compose ps

install_manager

cat <<EOF

采集器部署完成。

服务器标签：$SERVER_NAME
中心 Loki：$LOKI_URL

管理命令：
  alloy

远程服务器只需要允许出站 HTTPS 访问，无需开放新的入站端口。
EOF
}

install_manager() {
  if [ -e "$MANAGER_PATH" ] && ! grep -q "Xray Alloy Collector Manager" "$MANAGER_PATH" 2>/dev/null; then
    die "$MANAGER_PATH 已存在且不是本采集器管理脚本。请使用其他命令名称。"
  fi

  cat >"$MANAGER_PATH" <<'EOF'
#!/usr/bin/env bash
# Xray Alloy Collector Manager
set -Eeuo pipefail

STACK_DIR="${STACK_DIR:-/opt/xray-alloy-collector}"
COMPOSE_FILE="$STACK_DIR/compose.yaml"
INSTALL_SETTINGS_FILE="$STACK_DIR/.install.env"
INSTALLER_URL="https://raw.githubusercontent.com/xhpx7301/gla/main/deploy-xray-alloy-collector.sh"

die() { printf '错误: %s\n' "$*" >&2; exit 1; }
pause() { read -rp "按 Enter 键继续..." _; }
confirm() {
  local prompt="$1"
  read -rp "$prompt 输入 DELETE 确认: " answer
  [ "$answer" = "DELETE" ]
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
  printf '\n'
  docker system df
}

show_config() {
  [ -f "$STACK_DIR/alloy/config.alloy" ] || die "未找到采集器配置。"
  grep -E 'server   =|url =|__path__ =' "$STACK_DIR/alloy/config.alloy" || true
  printf '\n为安全起见，不会显示 Loki 密码。\n'
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
  printf '正在从 GitHub 下载最新版脚本并重新部署。请输入现有 Loki 密码以保留连接配置。\n'
  set -a
  # This file is created locally by the installer and intentionally excludes the password.
  . "$INSTALL_SETTINGS_FILE"
  set +a
  ALLOY_ACTION=install bash <(download_installer)
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
  docker image rm grafana/alloy:latest 2>/dev/null || true
  rm -rf "$STACK_DIR"
  rm -f "$0"
  printf 'Alloy 采集器已完整卸载。\n'
  exit 0
}

while true; do
  clear
  cat <<'MENU'
Xray Alloy 采集器管理

0. 退出
1. 安装或更新脚本并重新部署
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
    1) update_script_and_deploy; exit $? ;;
    2) compose up -d; pause ;;
    3) compose stop; pause ;;
    4) compose restart; pause ;;
    5) show_status; pause ;;
    6) compose logs -f --tail=100 alloy; pause ;;
    7) show_config; pause ;;
    8) update_collector; pause ;;
    9) uninstall_keep_data; pause ;;
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
Xray Alloy 采集器管理

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
