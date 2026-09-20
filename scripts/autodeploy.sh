#!/usr/bin/env bash
#
# Pull-based deploy agent. Runs on the VPS every minute from a systemd timer.
#
# Why pull and not push: the machine that develops this bot cannot open a
# connection to the server - no SSH client, and the egress policy blocks the
# ports. So the server does the reaching. It watches a branch, and when that
# branch moves it updates itself and reports the outcome back to GitHub.
#
# What it does NOT do: listen on a port, accept commands, or run anything that
# is not committed to the watched branch. There is no inbound surface.
#
# Know the trust model before installing this: whatever lands on the watched
# branch runs on this server within a minute. That is the same deal as any
# CI/CD pipeline, and it is the point - but it deserves saying plainly.
#
# Everything lives inside main() on purpose. The deploy rewrites this very
# file, and bash reads a script incrementally from disk; without the function
# wrapper, updating the agent mid-run makes bash resume at a byte offset that
# no longer means what it did a moment ago.

set -euo pipefail

main() {
    local repo_dir="${FLOWBOT_DIR:-/opt/flowbot}"
    local branch="${FLOWBOT_BRANCH:-claude/btc-momentum-trading-bot-wqxm70}"
    local status_file="${repo_dir}/data/deploy-status.json"
    local lock="/var/lock/flowbot-autodeploy.lock"

    # One deploy at a time: a slow image build must not overlap the next tick.
    exec 9>"${lock}"
    if ! flock -n 9; then
        log "another deploy is in progress, skipping"
        return 0
    fi

    cd "${repo_dir}"
    mkdir -p "${repo_dir}/data"
    git config --global --add safe.directory "${repo_dir}" 2>/dev/null || true

    if ! git fetch --quiet --prune origin "${branch}" 2>/dev/null; then
        log "fetch failed (network or auth); leaving the running build alone"
        return 0
    fi

    local local_sha remote_sha running reason=""
    local_sha="$(git rev-parse HEAD 2>/dev/null || echo none)"
    remote_sha="$(git rev-parse "origin/${branch}" 2>/dev/null || echo none)"
    running="$(docker compose ps --status running -q 2>/dev/null | wc -l || true)"

    if [[ "${local_sha}" != "${remote_sha}" ]]; then
        reason="new commit ${local_sha:0:8} -> ${remote_sha:0:8}"
    elif [[ "${running}" -eq 0 ]]; then
        reason="container is not running"
    else
        return 0                       # nothing to do; stay quiet
    fi

    log "deploying: ${reason}"
    local build_log
    build_log="$(mktemp)"

    # .env and data/ are gitignored, so a hard reset keeps the dashboard token,
    # the trade ledger and the recordings while guaranteeing the code matches
    # the branch exactly.
    git reset --hard "origin/${branch}" >>"${build_log}" 2>&1

    # Deliberately no --remove-orphans: this host runs other containers and
    # a scoped delete is not worth the risk for a flag we do not need.
    local deploy_ok=true
    if ! docker compose up -d --build >>"${build_log}" 2>&1; then
        deploy_ok=false
        log "docker compose failed"
    fi

    sleep 20                           # let it bind before asking how it is

    local port health
    port="$(grep -s '^HOST_PORT=' .env 2>/dev/null | cut -d= -f2 | tr -d '[:space:]' || true)"
    port="${port:-8033}"
    health="$(curl -fsS -m 5 "http://127.0.0.1:${port}/api/health" 2>/dev/null || echo '{}')"
    if [[ "${health}" == "{}" ]]; then
        health="$(docker exec "${CONTAINER_NAME:-flowbot-btc15m}" python -c \
            "import urllib.request;print(urllib.request.urlopen('http://127.0.0.1:8033/api/health',timeout=4).read().decode())" \
            2>/dev/null || echo '{}')"
    fi

    local container_log
    container_log="$(docker compose logs --tail 60 2>&1 | tail -c 12000 | base64 -w0 || true)"

    python3 "${repo_dir}/scripts/report_status.py" \
        --status-file "${status_file}" \
        --commit "$(git rev-parse HEAD)" \
        --branch "${branch}" \
        --reason "${reason}" \
        --ok "${deploy_ok}" \
        --health "${health}" \
        --build-log "${build_log}" \
        --compose-log "${container_log}" \
        || log "status report failed (the deploy itself is unaffected)"

    rm -f "${build_log}"
    log "deploy finished ok=${deploy_ok}"
}

log() { printf '%s %s\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$*"; }

main "$@"
