# Running flowbot on a VPS

The bot is a single container. It needs outbound HTTPS/WSS to the exchange,
one published port for the dashboard, and a writable directory for its ledger.
It never needs an exchange API key, because it cannot place a real order.

---

## One command

From a checkout on your machine:

```bash
./scripts/deploy.sh user@your-vps            # dashboard on :8033
./scripts/deploy.sh user@your-vps --port 8033 --path /opt/flowbot
```

The script checks SSH and Docker on the host, copies the repository (without
`.git`, `.venv`, `data` or tests), writes `/opt/flowbot/.env` with a dashboard
token — generating one if you have not set it, and reusing the existing one on
redeploys — builds the image, starts the container, waits for the health
endpoint, and prints the URL with the token in it.

Existing state in `<path>/data` is never deleted, so redeploying keeps the
equity curve and the trade ledger.

Useful flags:

| Flag | Why |
|---|---|
| `--venue simulator` | run the offline simulator first to check the plumbing |
| `--bind 127.0.0.1` | do not expose the port; reach it over an SSH tunnel |
| `--token …` | set the dashboard token explicitly |
| `--no-cache` | force a clean image rebuild |

---

## Pull-based auto-deploy (the server updates itself)

Use this when whoever ships the code cannot reach the server — which is the
normal case for an agent sandbox, a laptop behind CGNAT, or any network where
outbound SSH is blocked. Instead of pushing to the server, the server watches
a branch and updates itself.

Install it once, on the VPS:

```bash
sudo git clone -b claude/btc-momentum-trading-bot-wqxm70 \
  https://github.com/mypokelofi-collab/weatherbot.git /opt/flowbot
cd /opt/flowbot && sudo ./scripts/install-autodeploy.sh
```

The installer picks a free port (8033 upward), installs Docker if missing,
writes `.env` with a generated dashboard token, and enables a systemd timer
that runs `scripts/autodeploy.sh` every 60 seconds. It does not stop or
reconfigure anything already running on the host.

From then on: **push to the branch, and the server is running it within a
minute.** No inbound port, no SSH key handed out, nothing listening for
commands — the agent only ever pulls a specific branch of a specific repo.

```bash
systemctl status flowbot-autodeploy.timer   # is the agent alive
journalctl -u flowbot-autodeploy -n 50      # what it did last
cat /opt/flowbot/data/deploy-status.json    # the last deploy's outcome
systemctl disable --now flowbot-autodeploy.timer   # stop auto-updating
```

**Trust model, stated plainly:** anything committed to the watched branch runs
on the server within a minute. That is the point of the mechanism, and it is
the same arrangement as any CI/CD pipeline — but it means the branch is a
production credential. Protect it, and set `FLOWBOT_BRANCH` in
`/etc/flowbot-autodeploy.env` to a branch only trusted people can push to.

### Letting the shipper see what happened

Without extra setup the deploy outcome is written to
`/opt/flowbot/data/deploy-status.json` on the server. To make it visible to
whoever pushed the code, give the agent a GitHub fine-grained token with
**Contents: read and write** on this repository only, in
`/etc/flowbot-autodeploy.env`:

```
FLOWBOT_STATUS_TOKEN=github_pat_...
```

Each deploy then publishes commit, health, build log and container log to the
`deploy-status` branch. That branch is only ever written by the agent; it
never triggers a deploy. Revoke the token at any time — the bot keeps running,
it just stops reporting.

## From GitHub Actions (hands-off)

If you would rather not run anything locally, the repository ships
`.github/workflows/deploy.yml`. It runs the test suite, then the same
`scripts/deploy.sh`, from a GitHub runner — which has the outbound SSH a
Claude Code session does not.

Add four repository secrets (Settings → Secrets and variables → Actions):

| Secret | Value |
|---|---|
| `VPS_HOST` | the server's IP or hostname |
| `VPS_USER` | the SSH login, e.g. `root` |
| `VPS_SSH_KEY` | the **private** key, whole file including the BEGIN/END lines |
| `DASHBOARD_TOKEN` | any long random string; it gates the dashboard |

Then **Actions → Deploy to VPS → Run workflow**, picking the venue and whether
to publish the port. It is manual-trigger only — nothing deploys on a push —
and the run summary shows `docker compose ps` plus the last 40 log lines.
GitHub masks the secret values in the logs.

Generate a deploy key that only touches this server:

```bash
ssh-keygen -t ed25519 -f ~/.ssh/flowbot_deploy -C "flowbot deploy" -N ""
ssh-copy-id -i ~/.ssh/flowbot_deploy.pub root@your-vps
cat ~/.ssh/flowbot_deploy          # this is what goes in VPS_SSH_KEY
```

## By hand, if you prefer

```bash
ssh user@your-vps
git clone <this repo> /opt/flowbot && cd /opt/flowbot
cp .env.example .env && $EDITOR .env      # set FLOWBOT_SERVER__AUTH_TOKEN
docker compose up -d --build
docker compose logs -f
```

Prerequisites on the host:

```bash
curl -fsSL https://get.docker.com | sh
apt-get install -y docker-compose-plugin     # if not bundled
```

---

## After it is up

```bash
docker compose ps                  # health status
docker compose logs -f --tail 100  # what the bot is doing and why
docker compose restart             # restart, keeping state
docker compose down                # stop
ls -la data/state                  # flowbot.sqlite: orders, fills, trades, equity
```

The dashboard is at `http://your-vps:8033/?token=<token>`. `GET /api/health`
is open (for uptime monitors); everything else requires the token once one is
set.

---

## Securing the port

The dashboard exposes controls — pause, flatten, kill switch, parameter
edits — so treat the URL as a credential. Pick one:

**Firewall to your own IP**

```bash
ufw allow from <your-ip> to any port 8033 proto tcp
ufw deny 8033
```

**SSH tunnel (nothing exposed at all)**

```bash
./scripts/deploy.sh user@your-vps --bind 127.0.0.1
ssh -N -L 8033:127.0.0.1:8033 user@your-vps
# then open http://localhost:8033
```

**Reverse proxy with TLS** — put Caddy or nginx in front, terminate HTTPS on
your own hostname, and keep the token as a second factor. Any proxy must
forward websocket upgrade headers for `/ws`:

```caddy
your-host.example.com {
    reverse_proxy 127.0.0.1:8033
}
```

(Caddy forwards websockets by default; with nginx set `proxy_set_header
Upgrade $http_upgrade;` and `proxy_set_header Connection "upgrade";`.)

---

## Resources and housekeeping

* **CPU/RAM**: a 1 vCPU / 1 GB VPS is enough. The indicator windows are a few
  hundred bars of pure Python; the container idles well under 200 MB.
* **Disk**: the SQLite ledger grows a few MB per month. Recording the feed
  (`FLOWBOT_DATA__RECORD=true`) is what costs space — a few hundred MB per
  day for BTCUSDT — so only enable it when collecting backtest data, and prune
  `data/recordings/`.
* **Logs**: rotated by Docker at 10 MB × 5 files.
* **Clock**: the container runs UTC and the bar grid depends on it. Make sure
  the host has NTP running (`timedatectl` should say "System clock
  synchronized: yes").
* **Restarts**: `restart: unless-stopped`, so it comes back after a reboot.
  On restart the bot backfills 400 bars from the venue and starts a new
  session row in the ledger; an open paper position is *not* restored, by
  design — it is flat on boot and waits for the next signal.

---

## Upgrading

```bash
./scripts/deploy.sh user@your-vps      # re-run; data/ is preserved
```

or on the host: `git pull && docker compose up -d --build`.

---

## Checking it is real

Two things worth verifying on day one:

```bash
# 1. the venue and data provenance
curl -s 'http://your-vps:8033/api/health?token=…' | python3 -m json.tool
#    "real_data": true  -> live exchange feed
#    "real_data": false -> simulator; the dashboard also shows a banner

# 2. the book is live and synced
curl -s 'http://your-vps:8033/api/state?token=…' \
  | python3 -c 'import json,sys; d=json.load(sys.stdin); print(d["feed"], d["book"]["spread_bps"])'
```

If `feed.resyncs` climbs steadily, the host's network is dropping websocket
messages — the book is resyncing rather than drifting, which is the safe
failure, but it is worth fixing.
