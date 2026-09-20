#!/usr/bin/env bash
#
# One-command deploy of flowbot to a VPS over SSH.
#
#   ./scripts/deploy.sh user@your-vps
#   ./scripts/deploy.sh user@your-vps --port 8033 --path /opt/flowbot
#
# What it does, in order:
#   1. checks it can reach the host and that Docker is installed there
#   2. copies this repository (without .git, .venv, data or tests)
#   3. makes sure a dashboard token exists - generating one if not, because
#      an open port on the internet running a trading dashboard is not a
#      default anyone should get by accident
#   4. builds and starts the container with docker compose
#   5. waits for the health endpoint and prints the URL
#
# Existing trade history in <path>/data is never touched.

set -euo pipefail

TARGET="${1:-}"
REMOTE_PATH="/opt/flowbot"
HOST_PORT="8033"
BIND_ADDR="0.0.0.0"
TOKEN=""
VENUE=""
NO_BUILD_CACHE=""

usage() {
  cat <<USAGE
usage: $0 user@host [options]

  --path PATH        remote install directory (default: ${REMOTE_PATH})
  --port PORT        host port for the dashboard (default: ${HOST_PORT})
  --bind ADDR        address docker publishes on (default: ${BIND_ADDR};
                     use 127.0.0.1 to require an SSH tunnel)
  --token TOKEN      dashboard token (default: reuse existing, else generate)
  --venue VENUE      binance-futures | binance-spot | coinbase | simulator
  --no-cache         rebuild the image from scratch
  -h, --help         this message
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
    --venue)    VENUE="$2"; shift 2 ;;
    --no-cache) NO_BUILD_CACHE="--no-cache"; shift ;;
    -h|--help)  usage 0 ;;
    *) echo "unknown option: $1" >&2; usage 1 ;;
  esac
done

HOST_ONLY="${TARGET#*@}"
SSH="ssh -o BatchMode=yes -o ConnectTimeout=10"
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }

say "Checking ${TARGET}"
if ! $SSH "${TARGET}" true 2>/dev/null; then
  echo "cannot ssh to ${TARGET} without a password." >&2
  echo "add your key first:  ssh-copy-id ${TARGET}" >&2
  exit 1
fi

if ! $SSH "${TARGET}" "command -v docker >/dev/null"; then
  echo "docker is not installed on ${HOST_ONLY}." >&2
  echo "install it with:  curl -fsSL https://get.docker.com | sh" >&2
  exit 1
fi

if ! $SSH "${TARGET}" "docker compose version >/dev/null 2>&1"; then
  echo "the docker compose plugin is missing on ${HOST_ONLY}." >&2
  echo "install it with:  apt-get install -y docker-compose-plugin" >&2
  exit 1
fi

say "Checking port ${HOST_PORT} is free on ${HOST_ONLY}"
# A VPS usually has other things running on it. Finding out that the port is
# taken *after* building an image is a waste of everyone's time.
IN_USE="$($SSH "${TARGET}" "ss -ltnp 2>/dev/null | grep -E ':${HOST_PORT}[[:space:]]' || true")"
if [[ -n "${IN_USE}" ]]; then
  EXISTING="$($SSH "${TARGET}" "docker ps --filter 'publish=${HOST_PORT}' --format '{{.Names}}' 2>/dev/null | head -1" || true)"
  if [[ "${EXISTING}" == "flowbot" ]]; then
    echo "port ${HOST_PORT} is held by this bot's own container; it will be replaced."
  else
    echo "port ${HOST_PORT} on ${HOST_ONLY} is already in use:" >&2
    echo "${IN_USE}" >&2
    [[ -n "${EXISTING}" ]] && echo "(docker container: ${EXISTING})" >&2
    echo >&2
    echo "pick another one:  $0 ${TARGET} --port 8034" >&2
    exit 1
  fi
fi

say "Copying the repository to ${REMOTE_PATH}"
$SSH "${TARGET}" "mkdir -p '${REMOTE_PATH}/data/state' '${REMOTE_PATH}/data/recordings'"

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v rsync >/dev/null 2>&1; then
  rsync -az --delete \
    --exclude '.git' --exclude '.venv' --exclude 'data' \
    --exclude '__pycache__' --exclude '.pytest_cache' --exclude '*.pyc' \
    --exclude '.env' \
    "${REPO_ROOT}/" "${TARGET}:${REMOTE_PATH}/"
else
  # rsync is not always present; tar over ssh does the same job.
  tar -C "${REPO_ROOT}" \
      --exclude='.git' --exclude='.venv' --exclude='data' \
      --exclude='__pycache__' --exclude='.pytest_cache' --exclude='*.pyc' \
      --exclude='.env' -czf - . \
    | $SSH "${TARGET}" "tar -C '${REMOTE_PATH}' -xzf -"
fi

say "Preparing the environment file"
EXISTING_TOKEN="$($SSH "${TARGET}" "grep -s '^FLOWBOT_SERVER__AUTH_TOKEN=' '${REMOTE_PATH}/.env' | cut -d= -f2-" || true)"
if [[ -z "${TOKEN}" ]]; then
  if [[ -n "${EXISTING_TOKEN}" && "${EXISTING_TOKEN}" != "change-me-to-a-long-random-string" ]]; then
    TOKEN="${EXISTING_TOKEN}"
    echo "reusing the dashboard token already on the host"
  else
    TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    echo "generated a new dashboard token"
  fi
fi

REMOTE_ENV="$(mktemp)"
trap 'rm -f "${REMOTE_ENV}"' EXIT
{
  echo "# written by scripts/deploy.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "FLOWBOT_SERVER__AUTH_TOKEN=${TOKEN}"
  echo "BIND_ADDR=${BIND_ADDR}"
  echo "HOST_PORT=${HOST_PORT}"
  [[ -n "${VENUE}" ]] && echo "FLOWBOT_DATA__VENUE=${VENUE}"
  # Anything else the operator set on the host is preserved below.
} > "${REMOTE_ENV}"
$SSH "${TARGET}" "grep -sv -e '^FLOWBOT_SERVER__AUTH_TOKEN=' -e '^BIND_ADDR=' -e '^HOST_PORT=' -e '^#' ${VENUE:+-e '^FLOWBOT_DATA__VENUE='} '${REMOTE_PATH}/.env'" >> "${REMOTE_ENV}" || true
scp -q "${REMOTE_ENV}" "${TARGET}:${REMOTE_PATH}/.env"
$SSH "${TARGET}" "chmod 600 '${REMOTE_PATH}/.env'"

say "Building and starting the container"
$SSH "${TARGET}" "cd '${REMOTE_PATH}' && docker compose build ${NO_BUILD_CACHE} && docker compose up -d"

say "Waiting for the bot to come up"
for i in $(seq 1 30); do
  if $SSH "${TARGET}" "curl -fsS -m 3 'http://127.0.0.1:8033/api/health' >/dev/null 2>&1 \
       || docker exec flowbot python -c \"import urllib.request;urllib.request.urlopen('http://127.0.0.1:8033/api/health',timeout=3)\" >/dev/null 2>&1"; then
    break
  fi
  sleep 2
  [[ $i -eq 30 ]] && {
    echo "the bot did not report healthy in 60s. Recent logs:" >&2
    $SSH "${TARGET}" "cd '${REMOTE_PATH}' && docker compose logs --tail 40" >&2
    exit 1
  }
done

HEALTH="$($SSH "${TARGET}" "docker exec flowbot python -c \"import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8033/api/health',timeout=4).read().decode())\"" 2>/dev/null || echo '{}')"

cat <<DONE

  flowbot is running on ${HOST_ONLY}

  dashboard   http://${HOST_ONLY}:${HOST_PORT}/?token=${TOKEN}
  health      ${HEALTH}
  mode        PAPER MONEY - no exchange key is deployed, no real order can be sent

  logs        ssh ${TARGET} 'cd ${REMOTE_PATH} && docker compose logs -f'
  restart     ssh ${TARGET} 'cd ${REMOTE_PATH} && docker compose restart'
  stop        ssh ${TARGET} 'cd ${REMOTE_PATH} && docker compose down'
  ledger      ssh ${TARGET} 'ls -la ${REMOTE_PATH}/data/state'

DONE
if [[ "${BIND_ADDR}" == "0.0.0.0" ]]; then
  cat <<'WARN'
  The dashboard is reachable from the internet and protected only by that
  token. Consider one of:
    * a firewall rule allowing your IP only (ufw allow from <ip> to any port 8033)
    * --bind 127.0.0.1 plus an SSH tunnel:
        ssh -N -L 8033:127.0.0.1:8033 <target>

WARN
fi
