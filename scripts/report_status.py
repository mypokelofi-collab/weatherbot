#!/usr/bin/env python3
"""Report the outcome of a deploy back to GitHub, using stdlib only.

The machine that develops this bot can reach GitHub but cannot reach this
server. So the server publishes its deploy status to a branch in the same
repository, and the developer reads it from there. That closes the loop
without opening a single inbound port.

The status lands on a dedicated branch (default `deploy-status`) so it never
touches the code branch or triggers another deploy.

A token is optional: without one the status is still written to disk at
data/deploy-status.json, it is just not published.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import socket
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone

API = "https://api.github.com"


def api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    req = urllib.request.Request(
        f"{API}{path}",
        method=method,
        data=json.dumps(payload).encode() if payload else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "Content-Type": "application/json",
            "User-Agent": "flowbot-autodeploy",
        },
    )
    with urllib.request.urlopen(req, timeout=20) as r:
        body = r.read().decode()
        return json.loads(body) if body else {}


def ensure_branch(owner: str, repo: str, branch: str, token: str) -> None:
    """Create the status branch off the default branch if it does not exist."""
    try:
        api("GET", f"/repos/{owner}/{repo}/git/ref/heads/{branch}", token)
        return
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    info = api("GET", f"/repos/{owner}/{repo}", token)
    head = api("GET", f"/repos/{owner}/{repo}/git/ref/heads/{info['default_branch']}", token)
    api("POST", f"/repos/{owner}/{repo}/git/refs", token,
        {"ref": f"refs/heads/{branch}", "sha": head["object"]["sha"]})


def publish(owner: str, repo: str, branch: str, token: str, path: str, content: str) -> str:
    sha = None
    try:
        existing = api("GET", f"/repos/{owner}/{repo}/contents/{path}?ref={branch}", token)
        sha = existing.get("sha")
    except urllib.error.HTTPError as exc:
        if exc.code != 404:
            raise
    payload = {
        "message": f"deploy status {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%SZ')}",
        "content": base64.b64encode(content.encode()).decode(),
        "branch": branch,
    }
    if sha:
        payload["sha"] = sha
    result = api("PUT", f"/repos/{owner}/{repo}/contents/{path}", token, payload)
    return result.get("content", {}).get("html_url", "")


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--status-file", required=True)
    p.add_argument("--commit", default="")
    p.add_argument("--branch", default="")
    p.add_argument("--reason", default="")
    p.add_argument("--ok", default="true")
    p.add_argument("--health", default="{}")
    p.add_argument("--build-log", default="")
    p.add_argument("--compose-log", default="", help="base64 of recent container logs")
    args = p.parse_args()

    try:
        health = json.loads(args.health) if args.health.strip().startswith("{") else {}
    except json.JSONDecodeError:
        health = {}

    build_tail = ""
    if args.build_log and os.path.exists(args.build_log):
        with open(args.build_log, "r", errors="replace") as fh:
            build_tail = fh.read()[-8000:]

    compose_tail = ""
    if args.compose_log:
        try:
            compose_tail = base64.b64decode(args.compose_log).decode(errors="replace")
        except Exception:  # noqa: BLE001
            compose_tail = ""

    status = {
        "reported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "host": socket.gethostname(),
        "commit": args.commit,
        "branch": args.branch,
        "reason": args.reason,
        "deploy_ok": args.ok.lower() == "true",
        "health": health,
        "build_log_tail": build_tail,
        "container_log_tail": compose_tail,
    }
    text = json.dumps(status, indent=2)

    os.makedirs(os.path.dirname(args.status_file), exist_ok=True)
    with open(args.status_file, "w") as fh:
        fh.write(text)

    token = os.environ.get("FLOWBOT_STATUS_TOKEN", "").strip()
    slug = os.environ.get("FLOWBOT_REPO", "mypokelofi-collab/weatherbot").strip()
    status_branch = os.environ.get("FLOWBOT_STATUS_BRANCH", "deploy-status").strip()
    if not token:
        print("no FLOWBOT_STATUS_TOKEN set; status written locally only")
        return 0

    try:
        owner, repo = slug.split("/", 1)
        ensure_branch(owner, repo, status_branch, token)
        url = publish(owner, repo, status_branch, token, "deploy-status.json", text)
        print(f"published status to {url or status_branch}")
    except Exception as exc:  # noqa: BLE001 - never fail a deploy over reporting
        print(f"could not publish status: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
