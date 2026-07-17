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

die() { printf 'Error: %s\n' "$*" >&2; exit 1; }
note() { printf '\n==> %s\n' "$*"; }
hcl_escape() { sed -e 's/\\/\\\\/g' -e 's/"/\\"/g'; }

[ "$(id -u)" -eq 0 ] || die "Run this script as root: sudo bash $0"
[ -r "$XRAY_LOG" ] || die "Xray log not readable: $XRAY_LOG"
command -v docker >/dev/null 2>&1 || die "Docker is required."
docker info >/dev/null 2>&1 || die "Docker daemon is not running or is not accessible."

if docker compose version >/dev/null 2>&1; then
  COMPOSE=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE=(docker-compose)
else
  die "Docker Compose is required (docker compose or docker-compose)."
fi
compose() { "${COMPOSE[@]}" "$@"; }

[ -n "$SERVER_NAME" ] || die "Set SERVER_NAME, for example: SERVER_NAME=jp-tokyo-01"
[[ "$SERVER_NAME" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$ ]] || die "SERVER_NAME may only contain letters, numbers, dots, underscores, and hyphens."
[ -n "$LOKI_URL" ] || die "Set LOKI_URL, for example: https://loki.example.com/loki/api/v1/push"
[[ "$LOKI_URL" =~ ^https://.+/loki/api/v1/push$ ]] || die "LOKI_URL must be an HTTPS Loki push endpoint ending in /loki/api/v1/push."
[ -n "$LOKI_USERNAME" ] || die "LOKI_USERNAME must not be empty."

if [ -z "$LOKI_PASSWORD" ]; then
  read -rsp "Loki Basic Auth password: " LOKI_PASSWORD
  printf '\n'
fi
[ -n "$LOKI_PASSWORD" ] || die "LOKI_PASSWORD must not be empty."
case "$LOKI_PASSWORD" in
  *$'\n'*|*$'\r'*) die "LOKI_PASSWORD must not contain line breaks." ;;
esac

LOKI_URL_HCL="$(printf '%s' "$LOKI_URL" | hcl_escape)"
LOKI_USERNAME_HCL="$(printf '%s' "$LOKI_USERNAME" | hcl_escape)"
LOKI_PASSWORD_HCL="$(printf '%s' "$LOKI_PASSWORD" | hcl_escape)"

note "Creating collector files in $STACK_DIR"
install -d -m 0750 "$STACK_DIR/alloy"

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

note "Starting Alloy collector"
cd "$STACK_DIR"
compose pull
compose up -d

note "Verifying collector"
compose ps

cat <<EOF

Collector deployment complete.

Server label: $SERVER_NAME
Central Loki: $LOKI_URL

Verification:
  cd $STACK_DIR && docker compose logs -f alloy

The remote server needs outbound HTTPS access only. Do not open a new inbound port.
EOF
