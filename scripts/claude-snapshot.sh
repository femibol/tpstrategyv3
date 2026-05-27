#!/bin/bash
# Snapshot live VPS state to the `claude/live-state` branch every 5 min.
#
# Purpose: eliminate the copy-paste fatigue of pushing trade_history.json
# / signal_log.json / logs/trading.log to git every time Claude needs to
# review state. After this cron runs, fresh state is always at
# `origin/claude/live-state` — a Claude session just does:
#
#     git fetch origin claude/live-state
#     git show origin/claude/live-state:data/trade_history.json | jq ...
#     git show origin/claude/live-state:review/log-tail.log    | tail -200
#
# Uses a dedicated git worktree at /opt/trading-bot-snapshot so the push
# never touches the main checkout the bot reads from, never collides with
# the auto-deploy cron's branch-switching dance, and never re-deploys the
# bot on a snapshot commit.
#
# Installed by scripts/install-claude-bridge.sh; runs every 5 min via cron.

set -euo pipefail

REPO=/opt/trading-bot
SNAP=/opt/trading-bot-snapshot
BRANCH=claude/live-state

cd "$REPO"

# Worktree may not exist yet (first run after install) — create it.
if [ ! -d "$SNAP" ]; then
    git worktree add -B "$BRANCH" "$SNAP" origin/main >/dev/null 2>&1
fi

cd "$SNAP"

# Hard-reset to remote so divergence (history rewrites, force-pushes from
# elsewhere) can never wedge the cron. Falls back to local main if the
# remote branch doesn't exist yet (first install).
if git fetch origin "$BRANCH" >/dev/null 2>&1; then
    git reset --hard "origin/$BRANCH" >/dev/null
else
    git fetch origin main >/dev/null 2>&1 || true
    git reset --hard origin/main >/dev/null
fi

# Stage the live state — files are <200KB, race with the bot's writes is
# benign (worst case is a partial JSON which the next 5-min cycle replaces).
mkdir -p data review

for f in trade_history.json signal_log.json positions_state.json; do
    [ -f "$REPO/data/$f" ] && cp "$REPO/data/$f" "data/$f"
done

# Log tail — capacity-bound at 20k lines (~2MB) so the branch doesn't bloat.
# Try a couple of common paths so a deployment that writes logs to a
# non-default location still gets captured. Write a diagnostic if no path
# matches so the next session sees WHY the tail is empty instead of an
# invisible failure.
LOG_PATH=""
for candidate in "$REPO/logs/trading.log" "/var/log/trading-bot/trading.log"; do
    if [ -f "$candidate" ] && [ -r "$candidate" ]; then
        LOG_PATH="$candidate"
        break
    fi
done
if [ -n "$LOG_PATH" ]; then
    tail -n 20000 "$LOG_PATH" > review/log-tail.log
else
    {
        echo "no readable log file found. searched:"
        echo "  - $REPO/logs/trading.log"
        echo "  - /var/log/trading-bot/trading.log"
        echo "host dir listing:"
        ls -la "$REPO/logs/" 2>&1 || echo "  ($REPO/logs/ missing)"
    } > review/log-tail.log
fi

# Container state — single source of truth for "is anything wedged". MUST
# run from the real repo dir (docker compose needs the compose file in cwd),
# not the snapshot worktree which only has source files at HEAD — there's
# no docker-compose.yml in the worktree, so the previous version of this
# line silently produced an empty file.
( cd "$REPO" && docker compose ps --format json ) > review/docker-state.json 2>&1 || true
[ -s review/docker-state.json ] || \
    echo '{"error":"docker compose ps returned empty"}' > review/docker-state.json

# Snapshot metadata — useful for "how stale is this?" checks at the other end.
{
    echo "snapshot_utc=$(date -u +%FT%TZ)"
    echo "hostname=$(hostname)"
    echo "repo_head=$(cd "$REPO" && git rev-parse HEAD 2>/dev/null || echo unknown)"
    echo "bot_uptime=$(docker inspect -f '{{.State.StartedAt}}' trading-bot-trading-bot-1 2>/dev/null || echo unknown)"
} > review/snapshot-meta.txt

git add -A data review

# --allow-empty so a no-change tick still bumps the timestamp file — lets the
# consumer detect "is the cron still alive?" via the commit timestamp alone.
git -c user.email=snapshot@vps -c user.name=snapshot-cron \
    commit -m "snapshot: $(date -u +%FT%TZ)" --allow-empty --quiet

# Force-push because the branch is throwaway state. History on this branch
# is meaningless; only the latest commit matters.
git push --force --quiet origin "$BRANCH"
