#!/usr/bin/env bash
# Deploys a modular Alloy collector for security logs, host metrics, and Xray.
set -Eeuo pipefail

STACK_DIR="${STACK_DIR:-/opt/xray-alloy-collector}"
XRAY_LOG="${XRAY_LOG:-/var/log/x-ui/access.log}"
ENABLE_XRAY="${ENABLE_XRAY:-auto}"
ENABLE_SECURITY="${ENABLE_SECURITY:-true}"
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

  if [ -n "$METRICS_URL" ]; then
    [[ "$METRICS_URL" =~ ^https://.+/api/v1/write$ ]] || die "METRICS_URL 必须是以 /api/v1/write 结尾的 HTTPS 地址。"
    [ -n "$METRICS_USERNAME" ] || die "METRICS_USERNAME 不能为空。"
  fi
  if [ -n "$XUI_API_URL" ]; then
    [[ "$XUI_API_URL" =~ ^https://.+/panel/api/inbounds/list$ ]] || die "XUI_API_URL 必须是 HTTPS 且以 /panel/api/inbounds/list 结尾。"
    case "$XUI_API_URL" in *$'\n'*|*$'\r'*|*\"*) die "XUI_API_URL 包含不支持的字符。" ;; esac
    [ -n "$METRICS_URL" ] || die "启用 3x-ui API 采集时必须同时设置 METRICS_URL。"
  fi
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

  if [ -n "$METRICS_URL" ] && [ -z "$METRICS_PASSWORD" ]; then
    read -rsp "请输入指标接口 Basic Auth 密码: " METRICS_PASSWORD
    printf '\n'
  fi
  if [ -n "$METRICS_URL" ]; then
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
install -d -m 0750 "$STACK_DIR/alloy" "$STACK_DIR/assets" "$STACK_DIR/secrets"
write_install_settings

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
      - /proc:/host/proc:ro
      - /sys:/host/sys:ro
      - /:/host/root:ro,rslave
      - alloy-data:/var/lib/alloy/data
EOF

if [ "$ENABLE_XRAY" = true ]; then
  printf '      - %s:%s:ro\n' "$XRAY_LOG" "$XRAY_LOG" >>"$STACK_DIR/compose.yaml"
fi

if [ "$ENABLE_SECURITY" = true ]; then
  cat >>"$STACK_DIR/compose.yaml" <<EOF
      - $JOURNAL_PATH_HOST:/var/log/journal:ro
      - /etc/machine-id:/etc/machine-id:ro
      - /var/log:/host/var/log:ro
EOF
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
    expression = "^(?P<timestamp>\\\\d{4}/\\\\d{2}/\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2}\\\\.\\\\d+) from (?P<source>\\\\S+) accepted (?P<network>\\\\w+):(?P<destination>\\\\S+) \\\\[(?P<inbound>[^ ]+) (?:>>|->) (?P<outbound>[^\\\\]]+)\\\\](?: email: (?P<email>\\\\S+))?"
  }

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
  forward_to = [loki.write.central.receiver]
}

loki.source.journal "sshd" {
  path       = "/var/log/journal"
  matches    = "_SYSTEMD_UNIT=sshd.service"
  labels     = { job = "ssh-journal", server = "$SERVER_NAME" }
  forward_to = [loki.write.central.receiver]
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
    expression = "^(?P<timestamp>\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2},\\d+)"
  }

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

if [ -n "$METRICS_URL" ]; then
  cat >>"$STACK_DIR/alloy/config.alloy" <<EOF

prometheus.exporter.unix "host" {
  procfs_path = "/host/proc"
  sysfs_path  = "/host/sys"
  rootfs_path = "/host/root"
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

  if [ -n "$XUI_API_URL" ]; then
    cat >>"$STACK_DIR/alloy/config.alloy" <<EOF

prometheus.scrape "xui" {
  targets = [{
    __address__ = "xui-exporter:9105",
    job         = "xui-metrics",
    server      = "$SERVER_NAME",
  }]
  scrape_interval = "30s"
  forward_to      = [prometheus.remote_write.central.receiver]
}
EOF
  fi

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

note "正在验证采集器状态"
compose ps

install_manager

cat <<EOF

模块化采集器部署完成。

服务器标签：$SERVER_NAME
中心 Loki：$LOKI_URL
Xray 日志：$ENABLE_XRAY
安全日志：$ENABLE_SECURITY
主机指标：$([ -n "$METRICS_URL" ] && printf '已启用' || printf '未启用')
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
  docker image rm grafana/alloy:latest python:3.12-alpine 2>/dev/null || true
  rm -rf "$STACK_DIR"
  rm -f "$0"
  printf 'Alloy 采集器已完整卸载。\n'
  exit 0
}

while true; do
  clear
  cat <<'MENU'
GLA Alloy 模块化采集器管理

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
