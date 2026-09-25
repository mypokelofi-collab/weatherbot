#!/usr/bin/env bash
#
# Deploy pocketbot (bot + dashboard) to a VPS over SSH.
#
#   ./scripts/deploy-pocketbot.sh user@your-vps
#   ./scripts/deploy-pocketbot.sh user@your-vps --mode demo --port 8040
#
# The Pocket Option session string is read from the POCKETBOT_SSID
# environment variable (never a command-line flag, so it does not show up in
# `ps` or shell history). Without it, an SSID already on the host is kept; with
# neither, the bot runs a clearly labelled synthetic market.
#
# It installs into its own directory with its own compose project, so an
# existing flowbot deployment on the same host is not touched. The trade
# ledgers in <path>/data are never touched either.

set -euo pipefail

TARGET="${1:-}"
REMOTE_PATH="/opt/pocketbot"
HOST_PORT="8040"
BIND_ADDR="0.0.0.0"
TOKEN=""
MODE=""
ASSET=""
NO_BUILD_CACHE=""

usage() {
  cat <<USAGE
usage: $0 user@host [options]

  --path PATH     remote install directory (default: ${REMOTE_PATH})
  --port PORT     host port for the dashboard (default: ${HOST_PORT})
  --bind ADDR     address docker publishes on (default: ${BIND_ADDR};
                  use 127.0.0.1 to require an SSH tunnel)
  --token TOKEN   dashboard token (default: reuse existing, else generate)
  --mode MODE     paper | demo  (default: keep what the host has, else paper;
                  live is never set by this script - edit .env on the host)
  --asset ASSET   e.g. EURUSD_otc (default: keep, else the config file's)
  --no-cache      rebuild the image from scratch

  env POCKETBOT_SSID   Pocket Option session string to install (optional)
USAGE
  exit "${1:-0}"
}

[[ -z "${TARGET}" || "${TARGET}" == "-h" || "${TARGET}" == "--help" ]] && usage 0
shift

while [[ $# -gt 0 ]]; do
  case "$1" in
    --path)     REMOTE_PATH="$2"; shift 2 ;;
    --port)     HOST_PORT="$2"; shift 2 ;;
    --bind)     BIND_ADDR="$2"; shift 2 ;;
    --token)    TOKEN="$2"; shift 2 ;;
    --mode)     MODE="$2"; shift 2 ;;
    --asset)    ASSET="$2"; shift 2 ;;
    --no-cache) NO_BUILD_CACHE="--no-cache"; shift ;;
    -h|--help)  usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

case "${MODE}" in
  ""|paper|demo) ;;
  live) echo "refusing: real-money mode is never set by a deploy. Edit ${REMOTE_PATH}/.env on the host." >&2; exit 1 ;;
  *) echo "--mode must be paper or demo" >&2; exit 1 ;;
esac

HOST_ONLY="${TARGET#*@}"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
# Read one KEY=value from the host's .env without printing secrets to the log.
# Surrounding single quotes (how the SSID is stored) are stripped.
remote_env() {
  local v
  v="$($SSH "${TARGET}" "grep -s '^$1=' '${REMOTE_PATH}/.env' | tail -1 | cut -d= -f2-" || true)"
  v="${v#\'}"; v="${v%\'}"
  printf '%s' "${v}"
}

say "Checking ${TARGET}"
if ! $SSH "${TARGET}" true 2>/dev/null; then
  echo "cannot ssh to ${TARGET} without a password. add your key first: ssh-copy-id ${TARGET}" >&2
  exit 1
fi
$SSH "${TARGET}" "command -v docker >/dev/null" \
  || { echo "docker is not installed on ${HOST_ONLY}: curl -fsSL https://get.docker.com | sh" >&2; exit 1; }
$SSH "${TARGET}" "docker compose version >/dev/null 2>&1" \
  || { echo "the docker compose plugin is missing on ${HOST_ONLY}" >&2; exit 1; }

say "Checking port ${HOST_PORT} is free on ${HOST_ONLY}"
IN_USE="$($SSH "${TARGET}" "ss -ltnp 2>/dev/null | grep -E ':${HOST_PORT}[[:space:]]' || true")"
if [[ -n "${IN_USE}" ]]; then
  EXISTING="$($SSH "${TARGET}" "docker ps --filter 'publish=${HOST_PORT}' --format '{{.Names}}' 2>/dev/null | head -1" || true)"
  if [[ "${EXISTING}" == "pocketbot" ]]; then
    echo "port ${HOST_PORT} is held by pocketbot itself; it will be replaced."
  else
    echo "port ${HOST_PORT} on ${HOST_ONLY} is already in use:" >&2
    echo "${IN_USE}" >&2
    [[ -n "${EXISTING}" ]] && echo "(docker container: ${EXISTING})" >&2
    echo "pick another one:  $0 ${TARGET} --port 8041" >&2
    exit 1
  fi
fi

say "Copying the repository to ${REMOTE_PATH}"
$SSH "${TARGET}" "mkdir -p '${REMOTE_PATH}/data/pocketbot' && chown -R 10002:10002 '${REMOTE_PATH}/data/pocketbot' 2>/dev/null || true"
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# tar over ssh rather than rsync: it needs nothing installed on either end.
# Clear out the old code first so deleted files do not linger.
$SSH "${TARGET}" "cd '${REMOTE_PATH}' && rm -rf pocketbot flowbot config"
tar -C "${REPO_ROOT}" \
    --exclude='.git' --exclude='.venv' --exclude='data' --exclude='__pycache__' \
    --exclude='.pytest_cache' --exclude='*.pyc' --exclude='.env' --exclude='node_modules' \
    -czf - . \
  | $SSH "${TARGET}" "tar -C '${REMOTE_PATH}' -xzf -"

say "Preparing the environment file"
if [[ -z "${TOKEN}" ]]; then
  TOKEN="$(remote_env POCKETBOT_DASHBOARD_TOKEN)"
  if [[ -n "${TOKEN}" ]]; then
    echo "reusing the dashboard token already on the host"
  else
    TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "generated a new dashboard token"
  fi
fi
[[ -n "${MODE}" ]] || MODE="$(remote_env POCKETBOT_MODE)"
[[ -n "${MODE}" ]] || MODE="paper"
[[ -n "${ASSET}" ]] || ASSET="$(remote_env POCKETBOT_ASSET)"
SSID="${POCKETBOT_SSID:-}"
if [[ -n "${SSID}" ]]; then
  echo "installing the SSID from POCKETBOT_SSID"
else
  SSID="$(remote_env POCKETBOT_SSID)"
  [[ -n "${SSID}" ]] && echo "keeping the SSID already on the host" || echo "no SSID: the bot will run a synthetic market"
fi
if [[ "${MODE}" != "paper" && -z "${SSID}" ]]; then
  echo "mode ${MODE} needs an SSID; set POCKETBOT_SSID and run again" >&2
  exit 1
fi
REAL_ACK="$(remote_env POCKETBOT_REAL_MONEY)"

umask 077
REMOTE_ENV="$(mktemp)"
trap 'rm -f "${REMOTE_ENV}"' EXIT
{
  echo "# written by scripts/deploy-pocketbot.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "COMPOSE_FILE=docker-compose.pocket.yml"
  echo "POCKET_BIND_ADDR=${BIND_ADDR}"
  echo "POCKET_HOST_PORT=${HOST_PORT}"
  echo "POCKETBOT_DASHBOARD_TOKEN=${TOKEN}"
  echo "POCKETBOT_MODE=${MODE}"
  [[ -n "${ASSET}" ]] && echo "POCKETBOT_ASSET=${ASSET}"
  # Single-quoted: the SSID is JSON full of double quotes.
  [[ -n "${SSID}" ]] && printf "POCKETBOT_SSID='%s'\n" "${SSID}"
  # A real-money acknowledgement is only ever carried over, never created here.
  [[ -n "${REAL_ACK}" && "${MODE}" == "live" ]] && echo "POCKETBOT_REAL_MONEY=${REAL_ACK}"
} > "${REMOTE_ENV}"
scp -q "${REMOTE_ENV}" "${TARGET}:${REMOTE_PATH}/.env"
$SSH "${TARGET}" "chmod 600 '${REMOTE_PATH}/.env'"

say "Building and starting the container"
$SSH "${TARGET}" "cd '${REMOTE_PATH}' && docker compose build ${NO_BUILD_CACHE} && docker compose up -d"

say "Waiting for the dashboard"
HEALTH=""
for i in $(seq 1 30); do
  HEALTH="$($SSH "${TARGET}" "curl -fsS -m 3 'http://127.0.0.1:${HOST_PORT}/api/health' 2>/dev/null" || true)"
  [[ -n "${HEALTH}" ]] && break
  sleep 2
done
if [[ -z "${HEALTH}" ]]; then
  echo "pocketbot did not answer in 60s. Recent logs:" >&2
  $SSH "${TARGET}" "cd '${REMOTE_PATH}' && docker compose logs --tail 60" >&2
  exit 1
fi
# Give the first connection attempt a moment, then report what it says.
sleep 15
HEALTH="$($SSH "${TARGET}" "curl -fsS -m 3 'http://127.0.0.1:${HOST_PORT}/api/health'" 2>/dev/null || echo "${HEALTH}")"

cat <<DONE

  pocketbot is running on ${HOST_ONLY}

  dashboard   http://${HOST_ONLY}:${HOST_PORT}/?token=${TOKEN}
  health      ${HEALTH}
  mode        ${MODE}$( [[ -n "${SSID}" ]] && echo " (Pocket Option session installed)" || echo " (synthetic market - no SSID)" )

  logs        ssh ${TARGET} 'cd ${REMOTE_PATH} && docker compose logs -f'
  new SSID    POCKETBOT_SSID='42["auth",...]' $0 ${TARGET}
  stop        ssh ${TARGET} 'cd ${REMOTE_PATH} && docker compose down'

DONE
