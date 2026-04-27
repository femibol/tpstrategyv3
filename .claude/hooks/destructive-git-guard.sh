#!/usr/bin/env python3
"""
PreToolUse hook: block destructive bash commands that could nuke the bot's
audit trail or rewrite shared git history. Soft-deny with a reason; the user
can always run the command manually in a terminal if they really want it.
"""
import json
import re
import sys


PATTERNS = [
    (r"\bgit\s+push\s+(-f\b|--force\b)",
     "git push --force can rewrite shared history. Run manually in a terminal if intended."),
    (r"\bgit\s+reset\s+--hard\s+(origin/)?(main|master)\b",
     "git reset --hard onto main can drop unmerged work. Use git stash or a feature branch."),
    (r"\brm\s+-r?f?r?\s+(\./)?data(/|\b)",
     "rm on data/ would delete trade_history.json + signal_log.json (the bot's audit trail)."),
    (r"\brm\s+-r?f?r?\s+(\./)?logs(/|\b)",
     "rm on logs/ would delete trading.log + trades.log."),
    (r"\bdocker\s+compose\s+down\b",
     "docker compose down stops the live bot. Use `docker compose restart <service>` instead, or run down manually if intended."),
    (r"\bgit\s+branch\s+-D\b",
     "git branch -D force-deletes a branch and can lose unmerged commits. Use -d or push first."),
    (r"\bgit\s+clean\s+-[a-z]*f",
     "git clean -f permanently deletes untracked files. Run manually if intended."),
    (r"\bssh\b[^\n]*\brm\s+-r?f?r?\s+",
     "ssh ... rm -rf detected. The VPS holds the live trading data — run this manually if truly intended."),
    (r"\bssh\b[^\n]*\bdocker\s+compose\s+down\b",
     "ssh ... docker compose down would stop the live bot. Use vps_restart_service via the vps MCP instead."),
    (r"\bssh\b[^\n]*\bgit\s+reset\s+--hard\b",
     "ssh ... git reset --hard on the VPS can drop work. Use vps_git_pull via the vps MCP instead."),
    (r"\bscp\b[^\n]*\s/(data|logs)/",
     "scp targeting /data/ or /logs/ on the VPS. Confirm what's being overwritten before running manually."),
]


def main():
    try:
        data = json.load(sys.stdin)
    except Exception:
        sys.exit(0)

    if data.get("tool_name") != "Bash":
        sys.exit(0)

    cmd = data.get("tool_input", {}).get("command", "")
    if not cmd:
        sys.exit(0)

    for pattern, reason in PATTERNS:
        if re.search(pattern, cmd):
            sys.stderr.write(f"Blocked by destructive-git-guard: {reason}\n")
            sys.exit(2)

    sys.exit(0)


if __name__ == "__main__":
    main()
