#!/usr/bin/env bash
set -Eeuo pipefail

expected='expression = "^(?P<timestamp>\\d{4}-\\d{2}-\\d{2} \\d{2}:\\d{2}:\\d{2},\\d+)"'

render_expression() {
  local line
  while IFS= read -r line; do
    printf '%s\n' "$line"
  done <<EOF
    expression = "^(?P<timestamp>\\\\d{4}-\\\\d{2}-\\\\d{2} \\\\d{2}:\\\\d{2}:\\\\d{2},\\\\d+)"
EOF
}

actual="$(render_expression)"
actual="${actual#    }"

if [ "$actual" != "$expected" ]; then
  printf '生成的 Alloy 正则不正确。\n期望：%s\n实际：%s\n' "$expected" "$actual" >&2
  exit 1
fi

printf '生成的 Fail2ban Alloy 正则转义正确。\n'
