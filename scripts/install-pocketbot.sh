#!/usr/bin/env bash
#
# Install or update pocketbot + its dashboard, run ON THE VPS as root:
#
#   curl -fsSL https://raw.githubusercontent.com/mypokelofi-collab/weatherbot/claude/pocket-broker-trading-bot-nzszf3/scripts/install-pocketbot.sh | sudo bash
#
# Re-running it updates the code and keeps the token, SSID, mode and trade
# ledgers. It uses its own directory, compose project, container and port,
# so flowbot or anything else on the host is left alone.
#
# Settings (all optional, as environment variables):
#   POCKETBOT_MODE=paper|demo        default: keep, else paper
#   POCKETBOT_SSID='42["auth",...]'  default: keep, else ask (Enter skips)
#   POCKETBOT_PORT=8040              POCKETBOT_BIND=0.0.0.0
#   POCKETBOT_DIR=/opt/pocketbot     POCKETBOT_BRANCH=<this branch>

set -euo pipefail

main() {
    local repo="${POCKETBOT_REPO_URL:-https://github.com/mypokelofi-collab/weatherbot.git}"
    local branch="${POCKETBOT_BRANCH:-claude/pocket-broker-trading-bot-nzszf3}"
    local dir="${POCKETBOT_DIR:-/opt/pocketbot}"
    local port="${POCKETBOT_PORT:-}"
    local bind="${POCKETBOT_BIND:-}"
    local mode="${POCKETBOT_MODE:-}"
    local ssid="${POCKETBOT_SSID:-}"

    [[ "$(id -u)" -eq 0 ]] || die "run as root (sudo)"
    command -v docker >/dev/null || die "docker is not installed: curl -fsSL https://get.docker.com | sh"
    docker compose version >/dev/null 2>&1 || die "the docker compose plugin is missing: apt-get install -y docker-compose-plugin"
    command -v git >/dev/null || { say "Installing git"; apt-get update -qq && apt-get install -y -qq git; }

    say "Fetching ${branch}"
    if [[ -d "${dir}/.git" ]]; then
        git -C "${dir}" fetch --quiet origin "${branch}"
        # .env and data/ are gitignored, so this keeps settings and ledgers.
        git -C "${dir}" reset --quiet --hard "origin/${branch}"
    else
        [[ -e "${dir}" && -n "$(ls -A "${dir}" 2>/dev/null)" ]] && die "${dir} exists and is not a git checkout; set POCKETBOT_DIR"
        git clone --quiet --branch "${branch}" --single-branch "${repo}" "${dir}"
    fi
    cd "${dir}"
    mkdir -p data/pocketbot && chown -R 10002:10002 data/pocketbot

    say "Settings"
    local token
    token="$(env_get POCKETBOT_DASHBOARD_TOKEN)"
    [[ -n "${token}" ]] || token="$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    [[ -n "${mode}" ]] || mode="$(env_get POCKETBOT_MODE)"
    [[ -n "${mode}" ]] || mode="paper"
    case "${mode}" in paper|demo) ;; *) die "POCKETBOT_MODE must be paper or demo (real money is set by hand in ${dir}/.env)";; esac
    [[ -n "${port}" ]] || port="$(env_get POCKET_HOST_PORT)"
    [[ -n "${port}" ]] || port="8040"
    [[ -n "${bind}" ]] || bind="$(env_get POCKET_BIND_ADDR)"
    [[ -n "${bind}" ]] || bind="0.0.0.0"
    [[ -n "${ssid}" ]] || ssid="$(env_get POCKETBOT_SSID)"
    if [[ -z "${ssid}" && -r /dev/tty ]]; then
        echo "Paste your Pocket Option SSID (the 42[\"auth\",...] frame), or press Enter for a synthetic market:"
        IFS= read -rs ssid </dev/tty || true
        echo
    fi
    [[ "${mode}" == "paper" || -n "${ssid}" ]] || die "mode ${mode} needs an SSID"

    if ss -ltn 2>/dev/null | grep -qE ":${port}[[:space:]]" \
       && [[ "$(docker ps --filter "publish=${port}" --format '{{.Names}}' | head -1)" != "pocketbot" ]]; then
        die "port ${port} is already in use; run again with POCKETBOT_PORT=8041"
    fi

    local real_ack
    real_ack="$(env_get POCKETBOT_REAL_MONEY)"
    umask 077
    {
        echo "# written by scripts/install-pocketbot.sh on $(date -u +%Y-%m-%dT%H:%M:%SZ)"
        echo "COMPOSE_FILE=docker-compose.pocket.yml"
        echo "POCKET_BIND_ADDR=${bind}"
        echo "POCKET_HOST_PORT=${port}"
        echo "POCKETBOT_DASHBOARD_TOKEN=${token}"
        echo "POCKETBOT_MODE=${mode}"
        [[ -n "$(env_get POCKETBOT_ASSET)" ]] && echo "POCKETBOT_ASSET=$(env_get POCKETBOT_ASSET)"
        [[ -n "${ssid}" ]] && printf "POCKETBOT_SSID='%s'\n" "${ssid}"
        [[ -n "${real_ack}" ]] && echo "POCKETBOT_REAL_MONEY=${real_ack}"
    } > .env.new
    mv .env.new .env

    say "Building and starting"
    docker compose up -d --build

    say "Waiting for the dashboard"
    local health=""
    for _ in $(seq 1 30); do
        health="$(curl -fsS -m 3 "http://127.0.0.1:${port}/api/health" 2>/dev/null || true)"
        [[ -n "${health}" ]] && break
        sleep 2
    done
    [[ -n "${health}" ]] || { docker compose logs --tail 60; die "pocketbot did not come up"; }
    sleep 15
    health="$(curl -fsS -m 3 "http://127.0.0.1:${port}/api/health" 2>/dev/null || echo "${health}")"

    local ip
    ip="$(curl -fsS -m 4 https://api.ipify.org 2>/dev/null || hostname -I | awk '{print $1}')"
    cat <<DONE

  pocketbot is running

  dashboard   http://${ip}:${port}/?token=${token}
  health      ${health}
  mode        ${mode}$( [[ -n "${ssid}" ]] && echo " on Pocket Option" || echo " on a synthetic market (no SSID)" )

  logs        cd ${dir} && docker compose logs -f
  update      re-run this installer
  new SSID    POCKETBOT_SSID='42["auth",...]' re-run this installer
  stop        cd ${dir} && docker compose down

DONE
}

env_get() {
    local v
    v="$(grep -s "^$1=" .env 2>/dev/null | tail -1 | cut -d= -f2- || true)"
    v="${v#\'}"; v="${v%\'}"
    printf '%s' "${v}"
}
say() { printf '\n\033[1m==> %s\033[0m\n' "$*"; }
die() { printf '\033[31merror: %s\033[0m\n' "$*" >&2; exit 1; }

main "$@"
