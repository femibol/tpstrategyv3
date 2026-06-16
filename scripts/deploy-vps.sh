#!/bin/bash
#
# Safe VPS deploy: pull latest code from origin/main, restart bot.
#
# Why this exists: 2026-06-16 incident. The previous deploy command
# used `git reset --hard origin/main`, which is destructive of any
# working-tree path that origin's index doesn't claim. PR #215 had
# `git rm --cached data/trade_history.json` (the file moved to
# .gitignore), but the host still had a modified copy of that file
# in the working tree. When the reset ran, git removed the file
# because origin's view said it shouldn't be there. 297 trades of
# analytics history wiped; recovered from the claude/live-state
# snapshot branch.
#
# What this script does differently:
#
#   1. Backs up data/*.json to data-backup/ BEFORE touching git. Floor
#      against any future "git decides to delete this" surprise.
#   2. Stashes any local config/ edits (auto-tuner writes can land
#      there on pre-#212 code; harmless on overlay-pattern code).
#   3. Uses targeted `git checkout origin/main -- <code paths>` instead
#      of `git reset --hard`. Only code directories are touched. Data,
#      logs, runtime state are never modified by git.
#   4. Restarts the trading-bot container — bot/ is bind-mounted so a
#      restart picks up the new code without a rebuild.
#   5. Verifies dashboard /health responds before exiting OK.
#   6. Prunes old data backups (keeps last 10 per file).
#
# Usage on the VPS:
#   scripts/deploy-vps.sh
#
# Environment overrides:
#   REPO_DIR  (default: /opt/trading-bot)
#   CONTAINER (default: trading-bot-trading-bot-1)
#   BRANCH    (default: main)

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/trading-bot}"
CONTAINER="${CONTAINER:-trading-bot-trading-bot-1}"
BRANCH="${BRANCH:-main}"

cd "$REPO_DIR"

ts=$(date -u +%Y%m%dT%H%M%SZ)

# ---- 1. Backup data/*.json ----
mkdir -p data-backup
shopt -s nullglob
for f in data/*.json; do
    cp -a "$f" "data-backup/$(basename "$f").$ts.bak"
done
shopt -u nullglob
echo "deploy: backed up $(ls data-backup/*.${ts}.bak 2>/dev/null | wc -l) data/*.json files"

# ---- 2. Stash any local config/ edits ----
# The auto-tuner used to write to config/ files directly (pre-PR-#212).
# An uncommitted edit there would conflict with the upcoming checkout.
# Overlay-pattern code (#212+) writes only to data/, so this stash is
# usually a no-op — but it's the cheap defensive path.
if ! git diff --quiet -- config/ 2>/dev/null; then
    git stash push -u -m "pre-deploy auto-stash $ts" -- config/ >/dev/null 2>&1 || true
    echo "deploy: stashed config/ working-tree edits"
fi

# ---- 3. Fetch + targeted checkout ----
# CRITICAL: do NOT use `git reset --hard origin/<branch>`. That follows
# origin's view of which files should exist and will delete any locally-
# present file that origin has rm'd from tracking. This is what wiped
# data/trade_history.json on 2026-06-16.
#
# Instead, list explicit code paths and check them out. New code paths
# must be added to this list when they appear in the repo.
git fetch origin "$BRANCH" --quiet

CODE_PATHS=(
    bot
    tests
    scripts
    docs
    config
    CLAUDE.md
    HANDOFF.md
    README.md
    requirements.txt
    docker-compose.yml
    Dockerfile
    .gitignore
)
EXISTING=()
for p in "${CODE_PATHS[@]}"; do
    # Skip silently if the path doesn't exist in origin (e.g. config/ in
    # a future repo layout). Don't fail the deploy on a missing-by-design
    # path; just skip it.
    if git cat-file -e "origin/$BRANCH:$p" 2>/dev/null; then
        EXISTING+=("$p")
    fi
done
if [ ${#EXISTING[@]} -eq 0 ]; then
    echo "deploy: ERROR — no code paths matched in origin/$BRANCH" >&2
    exit 1
fi
git checkout "origin/$BRANCH" -- "${EXISTING[@]}"
# Move the branch tip too so subsequent `git status` is sane.
git update-ref "refs/heads/$BRANCH" "origin/$BRANCH"
git checkout "$BRANCH" --quiet 2>/dev/null || true
echo "deploy: checked out $(git rev-parse --short HEAD) on $BRANCH"

# ---- 4. Restart container ----
docker restart "$CONTAINER" >/dev/null
echo "deploy: container $CONTAINER restarted"

# ---- 5. Wait for /health ----
ok=""
for i in $(seq 1 20); do
    if curl -s -m 3 http://localhost:5000/health 2>/dev/null | grep -q '"status":"ok"'; then
        ok=1
        echo "deploy: dashboard ready after $((i*3))s"
        break
    fi
    sleep 3
done
if [ -z "$ok" ]; then
    echo "deploy: ERROR — dashboard didn't respond /health=ok after 60s" >&2
    echo "deploy: last 15 container log lines:" >&2
    docker logs --tail 15 "$CONTAINER" 2>&1 | sed 's/^/  /' >&2
    exit 1
fi

# ---- 6. Prune old backups ----
# Keep the 10 most recent per filename so disk doesn't fill over time.
(
    cd data-backup 2>/dev/null || exit 0
    for base in $(ls *.bak 2>/dev/null | sed 's/\.[0-9TZ]*\.bak$//' | sort -u); do
        ls -t "${base}".*.bak 2>/dev/null | tail -n +11 | xargs -r rm
    done
)

echo "deploy: ok"
