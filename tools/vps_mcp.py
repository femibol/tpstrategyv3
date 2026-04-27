#!/usr/bin/env python3
# /// script
# requires-python = ">=3.10"
# dependencies = ["mcp[cli]>=1.0.0"]
# ///
"""
VPS MCP server — narrow remote-ops tools for the trading bot's host.

Each tool wraps ONE specific shell command on the remote host. There is no
generic command-execution endpoint. To add a new operation, add a new tool
function — don't widen the surface of an existing one.

Configuration (env vars, read at startup):
- VPS_HOST          (required)  — IP or hostname
- VPS_USER          (optional)  — defaults to "root"
- VPS_SSH_KEY       (optional)  — path to private key. If unset, uses ssh agent / default keys.
- VPS_PROJECT_DIR   (optional)  — remote path of the bot repo. Defaults to "/root/tpstrategyv3".

Run:
    uv run tools/vps_mcp.py

Wired into Claude Code via .mcp.json.
"""
from __future__ import annotations

import os
import shlex
import subprocess
from typing import Optional

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("vps")

VPS_HOST = os.environ.get("VPS_HOST", "")
VPS_USER = os.environ.get("VPS_USER", "root")
VPS_SSH_KEY = os.environ.get("VPS_SSH_KEY", "")
VPS_PROJECT_DIR = os.environ.get("VPS_PROJECT_DIR", "/root/tpstrategyv3")
SSH_TIMEOUT_S = 30

# Narrow service whitelist — the only services these tools will operate on.
# Edit this list to expand; never accept caller-supplied service names blindly.
ALLOWED_SERVICES = {"trading-bot", "ib-gateway"}


def _ssh(remote_cmd: list[str], timeout: int = SSH_TIMEOUT_S) -> str:
    """Run a command on the VPS via ssh. remote_cmd is a list of args, NOT a
    shell string — so there's no opportunity for shell injection on the local
    side. The remote side does invoke a shell, so each arg is shlex-quoted."""
    if not VPS_HOST:
        return "ERROR: VPS_HOST env var is not set. Configure it in .env or your shell."

    ssh_cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={min(timeout, 10)}"]
    if VPS_SSH_KEY:
        ssh_cmd += ["-i", VPS_SSH_KEY]
    ssh_cmd.append(f"{VPS_USER}@{VPS_HOST}")
    ssh_cmd.append(" ".join(shlex.quote(a) for a in remote_cmd))

    try:
        result = subprocess.run(
            ssh_cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        out = result.stdout
        if result.stderr:
            out += "\n--- stderr ---\n" + result.stderr
        if result.returncode != 0:
            out += f"\n--- exit {result.returncode} ---"
        return out.strip() or "(no output)"
    except subprocess.TimeoutExpired:
        return f"ERROR: ssh timed out after {timeout}s"
    except FileNotFoundError:
        return "ERROR: ssh binary not found in PATH"
    except Exception as e:
        return f"ERROR: {type(e).__name__}: {e}"


# ---------- read-only tools ----------

@mcp.tool()
def vps_status() -> str:
    """Show the VPS git branch, last commit on that branch, system uptime, and
    container status (`docker compose ps`). Safe / read-only."""
    return _ssh([
        "bash", "-c",
        f"cd {VPS_PROJECT_DIR} && "
        "echo '## branch'   && git branch --show-current && "
        "echo '## HEAD'     && git log -1 --oneline && "
        "echo '## uptime'   && uptime && "
        "echo '## services' && docker compose ps"
    ])


@mcp.tool()
def vps_logs(service: str = "trading-bot", lines: int = 200) -> str:
    """Tail the last N lines of a docker compose service's logs. Read-only.
    `service` must be one of: trading-bot, ib-gateway. `lines` capped at 2000."""
    if service not in ALLOWED_SERVICES:
        return f"ERROR: service '{service}' not in allowlist: {sorted(ALLOWED_SERVICES)}"
    n = max(1, min(int(lines), 2000))
    return _ssh([
        "bash", "-c",
        f"cd {VPS_PROJECT_DIR} && docker compose logs {service} --tail {n}"
    ])


@mcp.tool()
def vps_grep_logs(pattern: str, service: str = "trading-bot", lines: int = 1000) -> str:
    """Grep recent docker compose logs for a pattern (case-insensitive). Read-only.
    Useful for hunting REJECTED, RATE LIMIT, IBKR NOT CONNECTED, BACKGROUND RECONNECT, etc."""
    if service not in ALLOWED_SERVICES:
        return f"ERROR: service '{service}' not in allowlist: {sorted(ALLOWED_SERVICES)}"
    n = max(1, min(int(lines), 5000))
    safe = pattern.replace("'", "'\\''")
    return _ssh([
        "bash", "-c",
        f"cd {VPS_PROJECT_DIR} && docker compose logs {service} --tail {n} 2>&1 | grep -iE '{safe}' | tail -200"
    ])


@mcp.tool()
def vps_disk() -> str:
    """Show disk usage on the VPS (`df -h` + bot data/logs sizes). Read-only."""
    return _ssh([
        "bash", "-c",
        f"df -h / && echo '---' && du -sh {VPS_PROJECT_DIR}/data {VPS_PROJECT_DIR}/logs 2>/dev/null"
    ])


@mcp.tool()
def vps_read_file(relpath: str, lines: int = 100) -> str:
    """Read up to N lines from a file on the VPS, relative to the project dir.
    Read-only. `relpath` must NOT contain '..' or absolute paths — only files
    inside the project tree."""
    if ".." in relpath or relpath.startswith("/"):
        return "ERROR: relpath must be relative and may not contain '..'"
    n = max(1, min(int(lines), 5000))
    return _ssh([
        "bash", "-c",
        f"cd {VPS_PROJECT_DIR} && head -{n} {shlex.quote(relpath)}"
    ])


# ---------- mutating tools (Claude Code prompts the user per call) ----------

@mcp.tool()
def vps_restart_service(service: str) -> str:
    """Restart a docker compose service on the VPS. MUTATING — interrupts the
    live bot for ~10s. Use only when the user explicitly asks.
    `service` must be one of: trading-bot, ib-gateway."""
    if service not in ALLOWED_SERVICES:
        return f"ERROR: service '{service}' not in allowlist: {sorted(ALLOWED_SERVICES)}"
    return _ssh([
        "bash", "-c",
        f"cd {VPS_PROJECT_DIR} && docker compose restart {service} && docker compose ps {service}"
    ], timeout=120)


@mcp.tool()
def vps_git_pull() -> str:
    """Pull latest commits on the VPS's current branch. MUTATING — code changes
    don't take effect until the trading-bot service is rebuilt + restarted."""
    return _ssh([
        "bash", "-c",
        f"cd {VPS_PROJECT_DIR} && git fetch origin && "
        "branch=$(git branch --show-current) && "
        'echo "branch: $branch" && git pull --ff-only origin "$branch" && git log -3 --oneline'
    ], timeout=60)


if __name__ == "__main__":
    mcp.run()
