#!/usr/bin/env bash
set -Eeuo pipefail

ACTION="${1:-install}"
SERVER_NAME="${SERVER_NAME:-}"
SSH_LOGIN_URL="${SSH_LOGIN_URL:-}"
SSH_LOGIN_KEY="${SSH_LOGIN_KEY:-}"
CONFIG_DIR=/etc/gla
CONFIG_FILE="$CONFIG_DIR/ssh-login.env"
AGENT_FILE=/usr/local/sbin/gla-ssh-login
PAM_FILE=/etc/pam.d/sshd
PAM_LINE='session optional pam_exec.so quiet type=open_session /usr/local/sbin/gla-ssh-login'

die() { printf '错误：%s\n' "$*" >&2; exit 1; }

if [ "$(id -u)" -ne 0 ]; then
  die "请使用 root 执行。"
fi

case "$ACTION" in
  install)
    [[ "$SERVER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die "请设置有效的 SERVER_NAME。"
    [[ "$SSH_LOGIN_URL" =~ ^https://[A-Za-z0-9.-]+(:[0-9]+)?/api/ssh-login$ ]] || die "SSH_LOGIN_URL 必须是 HTTPS 地址，并以 /api/ssh-login 结尾。"
    [[ "$SSH_LOGIN_KEY" =~ ^[A-Fa-f0-9]{64}$ ]] || die "SSH_LOGIN_KEY 必须是 64 位十六进制密钥。"
    command -v curl >/dev/null 2>&1 || die "需要 curl。"
    command -v systemd-run >/dev/null 2>&1 || die "需要 systemd-run。"
    [ -f "$PAM_FILE" ] || die "未找到 $PAM_FILE。"

    install -d -o root -g root -m 0700 "$CONFIG_DIR"
    printf 'GLA_SERVER_NAME=%q\nGLA_SSH_LOGIN_URL=%q\nGLA_SSH_LOGIN_KEY=%q\n' \
      "$SERVER_NAME" "$SSH_LOGIN_URL" "$SSH_LOGIN_KEY" >"$CONFIG_FILE"
    chmod 0600 "$CONFIG_FILE"

    cat >"$AGENT_FILE" <<'AGENT'
#!/usr/bin/env bash
set -u
[ "${PAM_TYPE:-}" = open_session ] || exit 0
[ -r /etc/gla/ssh-login.env ] || exit 0
# shellcheck disable=SC1091
. /etc/gla/ssh-login.env
source_ip="${PAM_RHOST:-}"
username="${PAM_USER:-}"
[ -n "$source_ip" ] && [ -n "$username" ] || exit 0
event_time="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
source_port="${SSH_CONNECTION#* }"
source_port="${source_port%% *}"
unit="gla-ssh-login-${$}-${RANDOM}"
systemd-run --quiet --no-block --collect --unit="$unit" \
  /usr/bin/curl --silent --show-error --fail --retry 3 --retry-delay 2 \
  --connect-timeout 2 --max-time 3 \
  -H "X-GLA-Key: $GLA_SSH_LOGIN_KEY" \
  --data-urlencode "server=$GLA_SERVER_NAME" \
  --data-urlencode "user=$username" \
  --data-urlencode "source_ip=$source_ip" \
  --data-urlencode "source_port=$source_port" \
  --data-urlencode "event_time=$event_time" \
  --data-urlencode "auth_method=unknown" \
  "$GLA_SSH_LOGIN_URL" >/dev/null 2>&1 || true
exit 0
AGENT
    chmod 0750 "$AGENT_FILE"
    if ! grep -Fqx "$PAM_LINE" "$PAM_FILE"; then
      cp -a "$PAM_FILE" "$PAM_FILE.gla-backup"
      printf '\n# GLA SSH successful login event\n%s\n' "$PAM_LINE" >>"$PAM_FILE"
    fi
    printf 'GLA SSH 登录事件采集已启用：%s\n' "$SERVER_NAME"
    printf 'SSH 登录不依赖中心端可用性；事件异步发送，发送失败不会影响登录。\n'
    ;;
  uninstall)
    if [ -f "$PAM_FILE" ]; then
      sed -i '\|^session optional pam_exec\.so quiet type=open_session /usr/local/sbin/gla-ssh-login$|d; /^# GLA SSH successful login event$/d' "$PAM_FILE"
    fi
    rm -f "$AGENT_FILE" "$CONFIG_FILE"
    printf 'GLA SSH 登录事件采集已移除。原 PAM 配置备份保留在 %s.gla-backup（如存在）。\n' "$PAM_FILE"
    ;;
  *) die "用法：$0 [install|uninstall]" ;;
esac
