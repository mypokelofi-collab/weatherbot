#!/usr/bin/env bash
#
# Run this ON THE VPS, once. It sets up the bot and a deploy agent that keeps
# it in sync with a branch on GitHub.
#
#   curl -fsSL https://raw.githubusercontent.com/mypokelofi-collab/weatherbot/claude/btc-momentum-trading-bot-wqxm70/scripts/install-autodeploy.sh | bash
#
# or, from a clone:  sudo ./scripts/install-autodeploy.sh
#
# It does not stop, restart or reconfigure anything already running on the
# host, and it picks a port nothing else is using.

set -euo pipefail

REPO_URL="${FLOWBOT_REPO_URL:-https://github.com/mypokelofi-collab/weatherbot.git}"
BRANCH="${FLOWBOT_BRANCH:-claude/btc-momentum-trading-bot-wqxm70}"
DIR="${FLOWBOT_DIR:-/opt/flowbot}"
PORT="${FLOWBOT_PORT:-}"
BIND="${FLOWBOT_BIND:-0.0.0.0}"
NAME="${CONTAINER_NAME:-flowbot}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run as root (sudo)"

say "Checking Docker"
if ! command -v docker >/dev/null; then
    say "Installing Docker"
    curl -fsSL https://get.docker.com | sh
fi
docker compose version >/dev/null 2>&1 || apt-get install -y docker-compose-plugin
docker compose version

say "Choosing a free port"
if [[ -z "${PORT}" ]]; then
    for candidate in 8033 8034 8035 8036 8037 8038; do
        if ! ss -ltn 2>/dev/null | grep -q ":${candidate} "; then
            PORT="${candidate}"
            break
        fi
    done
fi
[[ -n "${PORT}" ]] || die "no free port found in 8033-8038; set FLOWBOT_PORT"
ss -ltn 2>/dev/null | grep -q ":${PORT} " && die "port ${PORT} is already in use"
echo "using port ${PORT}"

if docker ps -a --format '{{.Names}}' | grep -qx "${NAME}"; then
    if [[ "$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "${NAME}" 2>/dev/null)" != "flowbot" ]]; then
        die "a container named ${NAME} already exists and is not ours; set CONTAINER_NAME"
    fi
fi

say "Fetching the code into ${DIR}"
if [[ -d "${DIR}/.git" ]]; then
    git -C "${DIR}" remote set-url origin "${REPO_URL}"
    git -C "${DIR}" fetch --quiet origin "${BRANCH}"
    git -C "${DIR}" checkout -q -B "${BRANCH}" "origin/${BRANCH}"
else
    mkdir -p "$(dirname "${DIR}")"
    git clone --quiet -b "${BRANCH}" "${REPO_URL}" "${DIR}"
fi
cd "${DIR}"
mkdir -p data/state data/recordings
git config --global --add safe.directory "${DIR}" 2>/dev/null || true

say "Writing ${DIR}/.env"
if [[ -f .env ]] && grep -q '^FLOWBOT_SERVER__AUTH_TOKEN=' .env; then
    echo "keeping the existing dashboard token"
    TOKEN="$(grep '^FLOWBOT_SERVER__AUTH_TOKEN=' .env | cut -d= -f2-)"
else
    TOKEN="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
fi
cat > .env <<ENVEOF
FLOWBOT_SERVER__AUTH_TOKEN=${TOKEN}
BIND_ADDR=${BIND}
HOST_PORT=${PORT}
CONTAINER_NAME=${NAME}
ENVEOF
chmod 600 .env

say "Installing the deploy agent"
install -m 0755 scripts/autodeploy.sh "${DIR}/scripts/autodeploy.sh"
cp deploy/flowbot-autodeploy.service /etc/systemd/system/
cp deploy/flowbot-autodeploy.timer /etc/systemd/system/
if [[ ! -f /etc/flowbot-autodeploy.env ]]; then
    cat > /etc/flowbot-autodeploy.env <<AGENTEOF
FLOWBOT_DIR=${DIR}
FLOWBOT_BRANCH=${BRANCH}
CONTAINER_NAME=${NAME}
FLOWBOT_REPO=mypokelofi-collab/weatherbot
FLOWBOT_STATUS_BRANCH=deploy-status
# Optional: a GitHub fine-grained token with Contents:read+write on this repo.
# With it the agent publishes each deploy's outcome to the deploy-status
# branch, so whoever ships the code can see what happened without logging in.
FLOWBOT_STATUS_TOKEN=
AGENTEOF
    chmod 600 /etc/flowbot-autodeploy.env
fi
systemctl daemon-reload
systemctl enable --now flowbot-autodeploy.timer

say "First deploy"
systemctl start flowbot-autodeploy.service || true
docker compose up -d --build

say "Opening the firewall, if one is active"
if command -v ufw >/dev/null && ufw status 2>/dev/null | grep -q "Status: active"; then
    ufw allow "${PORT}/tcp" || true
fi

sleep 15
HEALTH="$(curl -fsS -m 5 "http://127.0.0.1:${PORT}/api/health" 2>/dev/null || echo '{}')"
IP="$(curl -fsS -m 5 https://api.ipify.org 2>/dev/null || echo 'your-vps')"

cat <<DONE

  flowbot is installed.

  dashboard   http://${IP}:${PORT}/?token=${TOKEN}
  health      ${HEALTH}
  mode        PAPER MONEY - no exchange key, no real order is possible

  The deploy agent checks GitHub every 60s and updates the bot when the
  branch moves. Nothing listens for inbound commands.

  status      systemctl status flowbot-autodeploy.timer
  agent log   journalctl -u flowbot-autodeploy -n 50
  bot log     cd ${DIR} && docker compose logs -f
  stop agent  systemctl disable --now flowbot-autodeploy.timer

DONE
