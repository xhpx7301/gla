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
SSH_AUTH_INFO_FILE=/etc/ssh/sshd_config.d/00-gla-ssh-auth-info.conf
SSH_AUTH_INFO_MARKER=/etc/gla/ssh-auth-info-managed
SSH_INCLUDE_MARKER=/etc/gla/sshd-include-managed
SSHD_CONFIG_FILE=/etc/ssh/sshd_config
PAM_LINE='session optional pam_exec.so quiet type=open_session /usr/local/sbin/gla-ssh-login'

die() { printf '错误：%s\n' "$*" >&2; exit 1; }

restore_ssh_auth_config() {
  if [ -e "$SSH_AUTH_INFO_FILE.gla-backup" ]; then
    mv -f "$SSH_AUTH_INFO_FILE.gla-backup" "$SSH_AUTH_INFO_FILE"
  else
    rm -f "$SSH_AUTH_INFO_FILE"
  fi
  if [ -e "$SSH_INCLUDE_MARKER" ]; then
    sed -i '\|^Include /etc/ssh/sshd_config.d/\*\.conf$|d' "$SSHD_CONFIG_FILE"
    rm -f "$SSH_INCLUDE_MARKER"
  fi
  rm -f "$SSH_AUTH_INFO_MARKER"
}

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
    command -v sshd >/dev/null 2>&1 || die "未找到 sshd。"

    install -d -o root -g root -m 0700 "$CONFIG_DIR"
    if ! sshd -T 2>/dev/null | grep -Fqx 'exposeauthinfo yes'; then
      install -d -o root -g root -m 0755 "${SSH_AUTH_INFO_FILE%/*}"
      if ! grep -Eq '^[[:space:]]*Include[[:space:]]+/etc/ssh/sshd_config\.d/\*\.conf([[:space:]]|$)' "$SSHD_CONFIG_FILE"; then
        tmp_config="$(mktemp "${SSHD_CONFIG_FILE}.gla.XXXXXX")"
        printf 'Include /etc/ssh/sshd_config.d/*.conf\n' >"$tmp_config"
        cat "$SSHD_CONFIG_FILE" >>"$tmp_config"
        cat "$tmp_config" >"$SSHD_CONFIG_FILE"
        rm -f "$tmp_config"
        : >"$SSH_INCLUDE_MARKER"
      fi
      if [ -e "$SSH_AUTH_INFO_FILE" ]; then
        cp -a "$SSH_AUTH_INFO_FILE" "$SSH_AUTH_INFO_FILE.gla-backup"
      fi
      printf 'ExposeAuthInfo yes\n' >"$SSH_AUTH_INFO_FILE"
      chmod 0644 "$SSH_AUTH_INFO_FILE"
      if sshd_check="$(sshd -t 2>&1)"; then
        if sshd -T 2>/dev/null | grep -Fqx 'exposeauthinfo yes'; then
          : >"$SSH_AUTH_INFO_MARKER"
          chmod 0600 "$SSH_AUTH_INFO_MARKER"
          if command -v systemctl >/dev/null 2>&1; then
            systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
          fi
        else
          restore_ssh_auth_config
          printf '警告：sshd 未启用 ExposeAuthInfo，继续安装登录告警；认证方式可能显示“未知”。\n' >&2
        fi
      else
        restore_ssh_auth_config
        printf '警告：sshd 配置检查未通过，已撤销认证方式配置并继续安装登录告警；认证方式可能显示“未知”。\n' >&2
        [ -z "$sshd_check" ] || printf 'sshd 诊断：%s\n' "${sshd_check//$'\n'/; }" >&2
      fi
    fi

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
read -r _ source_port _ ssh_port <<<"${SSH_CONNECTION:-}"
ssh_port="${ssh_port:-22}"
auth_method=unknown
auth_methods=""
if [ -n "${SSH_USER_AUTH:-}" ] && [ -r "$SSH_USER_AUTH" ]; then
  auth_methods="$(awk 'NF { print $1 }' "$SSH_USER_AUTH" | sort -u | paste -sd, -)"
fi
if [ -z "$auth_methods" ] && command -v journalctl >/dev/null 2>&1; then
  auth_log="$(journalctl --since '2 minutes ago' -t sshd --no-pager -o cat 2>/dev/null || true)"
  for method in publickey password keyboard-interactive hostbased; do
    if grep -Fq "Accepted $method for $username from $source_ip " <<<"$auth_log"; then
      auth_methods="$method"
      break
    fi
  done
fi
case "$auth_methods" in
  publickey) auth_method='密钥认证' ;;
  password) auth_method='密码认证' ;;
  keyboard-interactive) auth_method='交互式认证' ;;
  hostbased) auth_method='主机认证' ;;
  publickey,*) auth_method='密钥及多重认证' ;;
  *publickey*) auth_method='密钥及多重认证' ;;
  *keyboard-interactive*) auth_method='交互式认证' ;;
  *password*) auth_method='密码及多重认证' ;;
  *) [ -z "$auth_methods" ] || auth_method="$auth_methods" ;;
esac
unit="gla-ssh-login-${$}-${RANDOM}"
systemd-run --quiet --no-block --collect --unit="$unit" \
  /usr/bin/curl --silent --show-error --fail --retry 3 --retry-delay 2 \
  --connect-timeout 2 --max-time 3 \
  -H "X-GLA-Key: $GLA_SSH_LOGIN_KEY" \
  --data-urlencode "server=$GLA_SERVER_NAME" \
  --data-urlencode "user=$username" \
  --data-urlencode "source_ip=$source_ip" \
  --data-urlencode "source_port=$source_port" \
  --data-urlencode "ssh_port=$ssh_port" \
  --data-urlencode "event_time=$event_time" \
  --data-urlencode "auth_method=$auth_method" \
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
    if [ -e "$SSH_AUTH_INFO_MARKER" ]; then
      if [ -e "$SSH_AUTH_INFO_FILE.gla-backup" ]; then
        mv -f "$SSH_AUTH_INFO_FILE.gla-backup" "$SSH_AUTH_INFO_FILE"
      else
        rm -f "$SSH_AUTH_INFO_FILE"
      fi
      rm -f "$SSH_AUTH_INFO_MARKER"
    fi
    if [ -e "$SSH_INCLUDE_MARKER" ]; then
      sed -i '\|^Include /etc/ssh/sshd_config.d/\*\.conf$|d' "$SSHD_CONFIG_FILE"
      rm -f "$SSH_INCLUDE_MARKER"
    fi
    if command -v sshd >/dev/null 2>&1 && sshd -t 2>/dev/null && command -v systemctl >/dev/null 2>&1; then
      systemctl reload ssh 2>/dev/null || systemctl reload sshd 2>/dev/null || true
    fi
    rm -f "$AGENT_FILE" "$CONFIG_FILE"
    printf 'GLA SSH 登录事件采集已移除。原 PAM 配置备份保留在 %s.gla-backup（如存在）。\n' "$PAM_FILE"
    ;;
  *) die "用法：$0 [install|uninstall]" ;;
esac
