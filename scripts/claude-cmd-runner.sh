#!/bin/bash
# Bidirectional command runner — polls `claude/cmd` every minute for new
# requests, executes them, pushes results back.
#
# Workflow:
#   1. Claude pushes a commit to origin/claude/cmd with `cmd/run.sh`
#      containing the command(s) to execute on the VPS.
#   2. This cron (every 60s) sees a new commit SHA, runs the script with
#      a 90s timeout, captures stdout+stderr+exit_code.
#   3. Result is committed back to `claude/cmd` as `cmd/result.txt` +
#      `cmd/exit_code` + `cmd/last_executed.txt` (the SHA we ran).
#   4. Claude pulls and reads the result.
#
# Latency: 60-120s per command (one cron interval + execution + push).
#
# Security model: scripts execute as root (same as the auto-deploy cron).
# Anyone with push access to the repo has operator-equivalent VPS access
# already — this gate just makes that access ergonomic, not broader. If
# tighter control is wanted later, add a SHA allowlist or signed commits.
#
# Installed by scripts/install-claude-bridge.sh.

set -euo pipefail

REPO=/opt/trading-bot
CMD_TREE=/opt/trading-bot-cmd
BRANCH=claude/cmd
LAST_SHA_FILE=/var/lib/claude-cmd-last-sha
TIMEOUT=90

cd "$REPO"

# Worktree on a dedicated branch — same isolation rationale as the snapshot
# script. Never touches main, never collides with auto-deploy.
if [ ! -d "$CMD_TREE" ]; then
    git worktree add -B "$BRANCH" "$CMD_TREE" origin/main >/dev/null 2>&1
fi

cd "$CMD_TREE"

# Fetch latest. If the branch doesn't exist yet, nothing to do — exit clean
# so the cron doesn't spam errors before Claude ever uses it.
if ! git fetch origin "$BRANCH" >/dev/null 2>&1; then
    exit 0
fi
git reset --hard "origin/$BRANCH" >/dev/null

# No command file = nothing to run.
[ -f cmd/run.sh ] || exit 0

REQUEST_SHA=$(git log -n 1 --format=%H -- cmd/run.sh 2>/dev/null || echo "")
[ -n "$REQUEST_SHA" ] || exit 0

LAST_SHA=$(cat "$LAST_SHA_FILE" 2>/dev/null || echo "")
if [ "$REQUEST_SHA" = "$LAST_SHA" ]; then
    # Already executed this command — wait for the next push.
    exit 0
fi

# Execute. Run from the REAL repo dir so commands like `tail logs/trading.log`
# resolve naturally; bind data/ and logs/ via the bot's existing paths.
TMP_OUT=$(mktemp)
TMP_RC=$(mktemp)
trap 'rm -f "$TMP_OUT" "$TMP_RC"' EXIT

(
    cd "$REPO"
    timeout --signal=TERM --kill-after=10 "$TIMEOUT" bash "$CMD_TREE/cmd/run.sh"
) > "$TMP_OUT" 2>&1 || echo "$?" > "$TMP_RC"

EXIT_CODE=$(cat "$TMP_RC" 2>/dev/null || echo "0")

# Write result + metadata back to the worktree.
mkdir -p cmd
{
    echo "=== executed: $REQUEST_SHA"
    echo "=== started:  $(date -u +%FT%TZ)"
    echo "=== exit:     $EXIT_CODE"
    echo "=== timeout:  ${TIMEOUT}s"
    echo "==="
    # Cap result at ~1MB so a runaway loop can't bloat the branch.
    head -c 1048576 "$TMP_OUT"
} > cmd/result.txt
echo "$EXIT_CODE" > cmd/exit_code
echo "$REQUEST_SHA" > cmd/last_executed.txt

git add -A cmd
git -c user.email=cmd@vps -c user.name=cmd-runner \
    commit -m "result: $REQUEST_SHA (exit $EXIT_CODE)" --quiet || exit 0
git push --quiet origin "$BRANCH"

echo "$REQUEST_SHA" > "$LAST_SHA_FILE"
