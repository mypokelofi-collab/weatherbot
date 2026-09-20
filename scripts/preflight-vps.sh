#!/usr/bin/env bash
#
# READ-ONLY inventory of a server before installing anything on it.
#
# Changes nothing. Starts nothing. Stops nothing. Run this first on a host
# that is already doing work, so the install can be checked against what is
# actually there instead of against assumptions.

set -uo pipefail

line() { printf '\n\033[1m── %s\033[0m\n' "$*"; }

echo "flowbot preflight · $(hostname) · $(date -u +%Y-%m-%dT%H:%M:%SZ)"

line "Host"
echo "os:      $(. /etc/os-release 2>/dev/null && echo "${PRETTY_NAME:-unknown}")"
echo "kernel:  $(uname -r)"
echo "uptime:  $(uptime -p 2>/dev/null || true)"
echo "cpu:     $(nproc) cores"
free -h 2>/dev/null | awk '/Mem:/{print "memory:  " $3 " used of " $2 " (" $7 " available)"}'
df -h / 2>/dev/null | awk 'NR==2{print "disk:    " $3 " used of " $2 " (" $4 " free)"}'
echo "clock:   $(timedatectl show -p NTPSynchronized --value 2>/dev/null || echo unknown) (NTP synchronized)"

line "Docker"
if command -v docker >/dev/null; then
    docker --version
    docker compose version 2>/dev/null || echo "compose plugin: MISSING (the installer would add it)"
    echo "daemon: $(systemctl is-active docker 2>/dev/null || echo unknown)"
else
    echo "docker: NOT INSTALLED (the installer would install it)"
fi

line "Containers already running"
docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null || echo "(cannot list)"

line "Containers stopped (their ports are still claimed when they restart)"
docker ps -a --filter status=exited --filter status=created --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}' 2>/dev/null | head -20 || true

line "Compose projects on this host"
docker ps -a --format '{{.Label "com.docker.compose.project"}}' 2>/dev/null \
    | grep -v '^$' | sort -u || echo "(none)"

line "Ports in use (listening now)"
ss -ltnp 2>/dev/null | awk 'NR>1{print $4}' | sed 's/.*://' | sort -un | tr '\n' ' ' || true
echo

line "Ports published by containers, running or not"
docker ps -a --format '{{.Ports}}' 2>/dev/null | grep -oE '0\.0\.0\.0:[0-9]+|:::[0-9]+|127\.0\.0\.1:[0-9]+' \
    | sed 's/.*://' | sort -un | tr '\n' ' ' || true
echo

line "Which port would flowbot take"
TAKEN="$( { ss -ltn 2>/dev/null | awk 'NR>1{print $4}' | sed 's/.*://';
            docker ps -a --format '{{.Ports}}' 2>/dev/null | grep -oE ':[0-9]+' | tr -d ':'; } | sort -un)"
for p in 8033 8034 8035 8036 8037 8038; do
    if ! grep -qx "$p" <<<"${TAKEN}"; then echo "first free port: ${p}"; break; fi
done

line "Name and path collisions"
docker ps -a --format '{{.Names}}' 2>/dev/null | grep -x 'flowbot-btc15m' \
    && echo "  ^ a container with our name already exists" || echo "container name flowbot-btc15m: free"
if [[ -e /opt/flowbot ]]; then
    if git -C /opt/flowbot remote get-url origin 2>/dev/null | grep -q weatherbot; then
        echo "/opt/flowbot: exists and is our repo (an update, not a fresh install)"
    else
        echo "/opt/flowbot: EXISTS AND IS SOMETHING ELSE - install would need another path"
    fi
else
    echo "/opt/flowbot: free"
fi
systemctl list-unit-files 2>/dev/null | grep -q '^flowbot-autodeploy' \
    && echo "systemd unit flowbot-autodeploy: ALREADY INSTALLED" \
    || echo "systemd unit flowbot-autodeploy: free"

line "Firewall"
ufw status 2>/dev/null | head -5 || echo "(ufw not installed)"

line "Outbound reachability the bot needs"
for host in api.binance.com fapi.binance.com github.com; do
    code="$(curl -s -o /dev/null -m 8 -w '%{http_code}' "https://${host}/" 2>/dev/null)"
    printf '%-20s HTTP %s%s\n' "${host}" "${code:-000}" \
        "$( [[ "${code}" == "451" ]] && echo '  <-- GEO-BLOCKED, this venue will not work here' )"
done

echo
echo "preflight complete - nothing was changed."
