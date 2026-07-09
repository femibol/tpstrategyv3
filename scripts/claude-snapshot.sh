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

# 2026-07-09 INCIDENT FIX: the old flow reset to origin/$BRANCH and
# committed ON TOP of it — so despite the force-push, the branch grew by
# one commit (carrying a ~2.5MB log blob) every 5 minutes, forever, both
# on GitHub and in the local .git. After ~5 weeks .git hit 70GB, filled
# the 79GB disk, and took down the bridge, the IB gateway, AND the bot
# (down 06-30 → 07-09). The snapshot branch is throwaway state: it must
# always be exactly ONE parentless commit. We build the tree from an
# empty index and commit-tree with NO parent — history physically cannot
# accumulate. No fetch of the old branch is needed since we replace it.
git read-tree --empty

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
    # 4k lines (~500KB), was 20k (~2.5MB) — the log blob was the payload
    # that made every 5-min commit expensive. 4k still covers several
    # hours of bot activity, and `claude-vps run` exists for deeper digs.
    tail -n 4000 "$LOG_PATH" > review/log-tail.log
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

# Force-add review/ because the repo's .gitignore has `*.log` which matches
# review/log-tail.log — without -f, git add silently skipped the log file
# and the snapshot branch never carried it. data/ is also gitignored
# (`data/*.csv`, `data/*.json`) — same -f reasoning. Confirmed against the
# first live install (PRs #177 / #178): the script was writing log-tail.log
# locally on every cron tick (3MB on disk) but git add -A was dropping it.
git add -fA data review

# Parentless commit via plumbing — the branch is REPLACED, never appended.
# (See the 2026-07-09 incident note above: append-per-tick grew .git to
# 70GB and killed the box.) snapshot-meta.txt changes every tick, so the
# commit timestamp still answers "is the cron alive?".
TREE=$(git write-tree)
COMMIT=$(git -c user.email=snapshot@vps -c user.name=snapshot-cron \
    commit-tree "$TREE" -m "snapshot: $(date -u +%FT%TZ)")
git update-ref "refs/heads/$BRANCH" "$COMMIT"

# Force-push because the branch is throwaway state. History on this branch
# is meaningless; only the latest commit matters — and now that's all
# there ever is.
git push --force --quiet origin "$BRANCH"

# Daily local GC (04:00-04:04 UTC tick): each replaced snapshot leaves the
# previous day's blobs dangling locally (~100-150MB/day). Without this the
# disk refills slowly even with parentless commits.
if [ "$(date -u +%H)" = "04" ] && [ "$(date -u +%M)" -lt 5 ]; then
    git -C "$REPO" reflog expire --expire=now --all >/dev/null 2>&1 || true
    git -C "$REPO" gc --prune=now --quiet >/dev/null 2>&1 || true
fi
