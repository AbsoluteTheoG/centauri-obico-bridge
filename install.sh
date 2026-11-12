#!/usr/bin/env bash
#
# centauri-obico-bridge installer
#
# Installs and starts the Elegoo Centauri → Moonraker bridge (and, by default,
# the moonraker-obico agent) with Docker. No firmware modification, fully
# reversible.
#
# Usage:
#   ./install.sh                 interactive install
#   ./install.sh --printer-ip 192.168.1.50 --obico-url https://app.obico.io --yes
#   ./install.sh --printer-model cc2 --access-code 123456 --printer-ip ...
#                                Centauri Carbon 2 (LAN Only Mode must be on)
#   ./install.sh --bridge-only   only run the bridge (moonraker-obico elsewhere)
#   ./install.sh --link          link/re-link the printer to Obico
#   ./install.sh --status        show container status and bridge health
#   ./install.sh --help
#
set -euo pipefail

# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"

if [ -t 1 ]; then
  C_RESET='\033[0m'; C_BOLD='\033[1m'; C_GREEN='\033[0;32m'
  C_YELLOW='\033[0;33m'; C_RED='\033[0;31m'; C_CYAN='\033[0;36m'
else
  C_RESET=''; C_BOLD=''; C_GREEN=''; C_YELLOW=''; C_RED=''; C_CYAN=''
fi

info()  { printf "${C_CYAN}==>${C_RESET} %s\n" "$*"; }
ok()    { printf "${C_GREEN}✓${C_RESET} %s\n" "$*"; }
warn()  { printf "${C_YELLOW}!${C_RESET} %s\n" "$*"; }
err()   { printf "${C_RED}✗${C_RESET} %s\n" "$*" >&2; }
die()   { err "$*"; exit 1; }

command_exists() { command -v "$1" >/dev/null 2>&1; }

# Detect "docker compose" (plugin) or "docker-compose" (standalone).
if docker compose version >/dev/null 2>&1; then
  DC=(docker compose)
elif command_exists docker-compose; then
  DC=(docker-compose)
else
  DC=()
fi

dc() { "${DC[@]}" "$@"; }

# --------------------------------------------------------------------------- #
# Arguments
# --------------------------------------------------------------------------- #

PRINTER_IP=""
PRINTER_MODEL=""
ACCESS_CODE=""
OBICO_URL=""
BRIDGE_ONLY=0
ASSUME_YES=0
DO_LINK=0
DO_STATUS=0

while [ $# -gt 0 ]; do
  case "$1" in
    --printer-ip) PRINTER_IP="${2:-}"; shift 2 ;;
    --printer-ip=*) PRINTER_IP="${1#*=}"; shift ;;
    --printer-model) PRINTER_MODEL="${2:-}"; shift 2 ;;
    --printer-model=*) PRINTER_MODEL="${1#*=}"; shift ;;
    --access-code) ACCESS_CODE="${2:-}"; shift 2 ;;
    --access-code=*) ACCESS_CODE="${1#*=}"; shift ;;
    --obico-url) OBICO_URL="${2:-}"; shift 2 ;;
    --obico-url=*) OBICO_URL="${1#*=}"; shift ;;
    --bridge-only) BRIDGE_ONLY=1; shift ;;
    --yes|-y) ASSUME_YES=1; shift ;;
    --link) DO_LINK=1; shift ;;
    --status) DO_STATUS=1; shift ;;
    --help|-h) sed -n '2,18p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) die "Unknown option: $1 (use --help)" ;;
  esac
done

# --------------------------------------------------------------------------- #
# Preflight
# --------------------------------------------------------------------------- #

require_docker() {
  if ! command_exists docker; then
    warn "Docker is not installed."
    if [ "$ASSUME_YES" -eq 0 ]; then
      printf "Install Docker now using the official convenience script? [y/N] "
      read -r answer
      case "$answer" in y|Y) ;; *) die "Docker is required. Aborting." ;; esac
    fi
    if [ "$(id -u)" -ne 0 ]; then
      command_exists sudo || die "sudo is required to install Docker."
      curl -fsSL https://get.docker.com | sudo sh
      sudo usermod -aG docker "$USER" || true
      warn "Docker installed. You may need to log out/in for group changes."
    else
      curl -fsSL https://get.docker.com | sh
    fi
  fi
  if ! docker info >/dev/null 2>&1; then
    die "Cannot talk to the Docker daemon. Is it running? (try: sudo systemctl start docker)"
  fi
  if [ "${#DC[@]}" -eq 0 ]; then
    die "Docker Compose v2 is required (docker compose). Install the compose plugin."
  fi
  ok "Docker and Compose are available."
}

# --------------------------------------------------------------------------- #
# Status
# --------------------------------------------------------------------------- #

show_status() {
  info "Container status"
  dc ps || true
  echo
  info "Bridge health (http://127.0.0.1:7125/health)"
  if command_exists curl; then
    curl -fsS http://127.0.0.1:7125/health && echo || warn "Bridge is not responding yet."
  else
    warn "curl not found, skipping health check."
  fi
}

# --------------------------------------------------------------------------- #
# Linking
# --------------------------------------------------------------------------- #

link_printer() {
  [ -f config/moonraker-obico.cfg ] || die "config/moonraker-obico.cfg not found. Run ./install.sh first."
  info "Stopping moonraker-obico to free the discovery port..."
  dc stop moonraker-obico >/dev/null 2>&1 || true
  info "Starting the interactive Obico linking. Open the Obico app/web UI,"
  info "add a Klipper printer and enter the 6-digit code when asked."
  docker run --rm -it --network host \
    -v "$SCRIPT_DIR/config:/opt/printer_data/config" \
    --entrypoint /opt/venv/bin/python \
    ghcr.io/thespaghettidetective/moonraker-obico:latest \
    -m moonraker_obico.link -c /opt/printer_data/config/moonraker-obico.cfg
  info "Restarting moonraker-obico..."
  dc up -d moonraker-obico >/dev/null 2>&1 || true
  ok "Linking finished."
}

# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

require_docker

if [ "$DO_STATUS" -eq 1 ]; then
  show_status
  exit 0
fi

if [ "$DO_LINK" -eq 1 ]; then
  link_printer
  exit 0
fi

printf "${C_BOLD}\nElegoo Centauri → Obico bridge installer${C_RESET}\n\n"

# Load defaults from an existing .env if present.
if [ -f .env ]; then
  # shellcheck disable=SC1091
  set -a; . ./.env; set +a
  PRINTER_IP="${PRINTER_IP:-${CENTAURI_HOST:-}}"
  if [ -z "$PRINTER_MODEL" ] && [ "${PRINTER_PROTOCOL:-}" = "cc2" ]; then
    PRINTER_MODEL="cc2"
  fi
  ACCESS_CODE="${ACCESS_CODE:-${CENTAURI_ACCESS_CODE:-}}"
  OBICO_URL="${OBICO_URL:-${OBICO_SERVER_URL:-}}"
fi

# Printer IP.
if [ -z "$PRINTER_IP" ]; then
  if [ "$ASSUME_YES" -eq 1 ]; then
    die "--printer-ip is required with --yes"
  fi
  printf "Printer IP address (e.g. 192.168.1.50): "
  read -r PRINTER_IP
fi
[ -n "$PRINTER_IP" ] || die "Printer IP is required."

# Printer model: Centauri Carbon (SDCP) or Centauri Carbon 2 (MQTT).
if [ -z "$PRINTER_MODEL" ]; then
  if [ "$ASSUME_YES" -eq 1 ]; then
    PRINTER_MODEL="cc"
  else
    printf "Printer model: 1) Centauri Carbon  2) Centauri Carbon 2 [1]: "
    read -r answer
    case "$answer" in 2|cc2|CC2) PRINTER_MODEL="cc2" ;; *) PRINTER_MODEL="cc" ;; esac
  fi
fi
case "$PRINTER_MODEL" in
  cc|sdcp) PRINTER_MODEL="cc" ;;
  cc2) ;;
  *) die "Unknown --printer-model '$PRINTER_MODEL' (use cc or cc2)" ;;
esac

if [ "$PRINTER_MODEL" = "cc2" ] && [ -z "$ACCESS_CODE" ]; then
  if [ "$ASSUME_YES" -eq 1 ]; then
    die "--access-code is required for the Centauri Carbon 2 with --yes"
  fi
  printf "Access code (printer: Settings → Network → LAN Only Mode): "
  read -r ACCESS_CODE
  [ -n "$ACCESS_CODE" ] || die "The access code is required for the Centauri Carbon 2."
fi

# Obico URL (only relevant for the full stack).
if [ "$BRIDGE_ONLY" -eq 0 ] && [ -z "$OBICO_URL" ]; then
  if [ "$ASSUME_YES" -eq 1 ]; then
    OBICO_URL="https://app.obico.io"
  else
    printf "Obico server URL [https://app.obico.io]: "
    read -r OBICO_URL
    OBICO_URL="${OBICO_URL:-https://app.obico.io}"
  fi
fi

info "Writing .env (printer: $PRINTER_IP, model: $PRINTER_MODEL)"
if [ "$PRINTER_MODEL" = "cc2" ]; then
  (umask 077; cat > .env <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
PRINTER_PROTOCOL=cc2
CENTAURI_HOST=$PRINTER_IP
CENTAURI_ACCESS_CODE=$ACCESS_CODE
START_CALIBRATION=1
OBICO_SERVER_URL=${OBICO_URL:-https://app.obico.io}
EOF
  )
else
  cat > .env <<EOF
# Generated by install.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)
PRINTER_PROTOCOL=sdcp
CENTAURI_HOST=$PRINTER_IP
CENTAURI_SDCP_PORT=3030
CENTAURI_VIDEO_PORT=3031
START_CALIBRATION=1
OBICO_SERVER_URL=${OBICO_URL:-https://app.obico.io}
EOF
fi

mkdir -p config data/bridge data/logs

if [ "$BRIDGE_ONLY" -eq 0 ]; then
  if [ ! -f config/moonraker-obico.cfg ]; then
    info "Generating config/moonraker-obico.cfg"
    sed "s|__OBICO_SERVER_URL__|${OBICO_URL:-https://app.obico.io}|g" \
      config/moonraker-obico.cfg.template > config/moonraker-obico.cfg
  else
    info "Keeping existing config/moonraker-obico.cfg"
  fi
fi

# moonraker-obico runs as uid 1000 and needs to write the auth token / logs.
chmod -R a+rwX config data 2>/dev/null || true

COMPOSE_FILE="docker-compose.yml"
[ "$BRIDGE_ONLY" -eq 1 ] && COMPOSE_FILE="docker-compose.bridge-only.yml"

info "Building and starting containers ($COMPOSE_FILE)..."
dc -f "$COMPOSE_FILE" up -d --build

info "Waiting for the bridge to become healthy..."
for _ in $(seq 1 30); do
  if command_exists curl && curl -fsS http://127.0.0.1:7125/health >/dev/null 2>&1; then
    break
  fi
  sleep 2
done

echo
if command_exists curl; then
  if curl -fsS http://127.0.0.1:7125/health >/dev/null 2>&1; then
    ok "Bridge is up: http://127.0.0.1:7125"
  else
    warn "Bridge is not responding yet. Check: dc -f $COMPOSE_FILE logs -f"
  fi
fi

echo
printf "${C_BOLD}Next steps${C_RESET}\n"
if [ "$BRIDGE_ONLY" -eq 1 ]; then
  cat <<'EOF'
  • Point your existing moonraker-obico at this host on port 7125.
  • The camera is at http://<this-host>:7125/webcam/stream
    and http://<this-host>:7125/webcam/snapshot
EOF
else
  cat <<EOF
  • Open your Obico app/web UI and add a Klipper printer, then link it.
    If the printer is not discovered automatically, run:
        ./install.sh --link
    and enter the 6-digit code shown by Obico.

  • Useful commands:
        ./install.sh --status          show containers + bridge health
        docker compose logs -f         follow all logs
        docker compose logs -f centauri-bridge
        docker compose down            stop everything (reversible)
EOF
fi
echo
ok "Done."
