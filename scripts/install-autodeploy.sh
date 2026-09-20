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
NAME="${CONTAINER_NAME:-flowbot-btc15m}"

say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

[[ "$(id -u)" -eq 0 ]] || die "run as root (sudo)"

say "Checking Docker"
# Never reinstall or restart Docker on a host that is already using it -
# that would bounce every other container on the machine.
if command -v docker >/dev/null; then
    echo "Docker is already installed; leaving it exactly as it is."
    docker --version
    RUNNING_COUNT="$(docker ps -q 2>/dev/null | wc -l || echo 0)"
    echo "${RUNNING_COUNT} container(s) already running - none will be touched."
else
    say "Installing Docker (not currently present)"
    curl -fsSL https://get.docker.com | sh
fi
if ! docker compose version >/dev/null 2>&1; then
    echo "adding the compose plugin (additive; does not restart the daemon)"
    apt-get install -y docker-compose-plugin
fi
docker compose version

say "Choosing a free port"
# A stopped container still owns its published port the moment it restarts,
# so both lists matter on a host running several bots.
TAKEN="$( { ss -ltn 2>/dev/null | awk 'NR>1{print $4}' | sed 's/.*://';
            docker ps -a --format '{{.Ports}}' 2>/dev/null | grep -oE ':[0-9]+' | tr -d ':'; } \
          | sort -un )"
if [[ -z "${PORT}" ]]; then
    for candidate in 8033 8034 8035 8036 8037 8038; do
        if ! grep -qx "${candidate}" <<<"${TAKEN}"; then
            PORT="${candidate}"
            break
        fi
    done
fi
[[ -n "${PORT}" ]] || die "no free port in 8033-8038 on this host; set FLOWBOT_PORT to one you know is free"
if grep -qx "${PORT}" <<<"${TAKEN}"; then
    echo "port ${PORT} is claimed by:" >&2
    ss -ltnp 2>/dev/null | grep ":${PORT} " >&2 || true
    docker ps -a --format '{{.Names}}\t{{.Ports}}' 2>/dev/null | grep ":${PORT}->" >&2 || true
    die "pick another port with FLOWBOT_PORT=<n>"
fi
echo "using port ${PORT}"

say "Checking for collisions with what is already on this host"
if docker ps -a --format '{{.Names}}' 2>/dev/null | grep -qx "${NAME}"; then
    PROJECT="$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project"}}' "${NAME}" 2>/dev/null || true)"
    if [[ "${PROJECT}" != "flowbot-btc15m" ]]; then
        die "a container named ${NAME} already exists and belongs to '${PROJECT:-another owner}'; set CONTAINER_NAME to something else"
    fi
    echo "found our own previous container; it will be replaced"
fi

if [[ -e "${DIR}" && ! -d "${DIR}/.git" ]]; then
    die "${DIR} already exists and is not a git checkout; set FLOWBOT_DIR to another path"
fi
if [[ -d "${DIR}/.git" ]] && ! git -C "${DIR}" remote get-url origin 2>/dev/null | grep -q "weatherbot"; then
    die "${DIR} is a git checkout of something else; set FLOWBOT_DIR to another path"
fi

if systemctl list-unit-files 2>/dev/null | grep -q '^flowbot-autodeploy'; then
    if ! grep -qs "${DIR}" /etc/systemd/system/flowbot-autodeploy.service; then
        die "a flowbot-autodeploy unit is already installed and points somewhere else; remove it first"
    fi
    echo "our deploy agent is already installed; it will be updated"
fi
echo "no collisions"

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
