#!/bin/bash
# ===========================================================
# Auto-Deploy: Pull latest code and restart the trading bot
# ===========================================================
#
# Two ways to use this:
#
# 1. CRON (simplest — checks every 5 min):
#    crontab -e
#    */5 * * * * /opt/trading-bot/deploy/auto-deploy.sh >> /var/log/auto-deploy.log 2>&1
#
# 2. MANUAL (after pushing code):
#    ssh root@YOUR_IP '/opt/trading-bot/deploy/auto-deploy.sh'
#
# What it does:
#   - Pulls latest code from GitHub (main branch)
#   - If there are changes, rebuilds and restarts the bot container
#   - If no changes, does nothing (safe to run frequently)
#   - Logs everything to stdout (redirect to file via cron)
#
# ===========================================================

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/trading-bot}"
BRANCH="${DEPLOY_BRANCH:-main}"
LOG_PREFIX="[auto-deploy]"
# Minimum seconds between successful container recreates. A burst of
# commits (typical during active dev — 3 commits in 15 min on 2026-05-16
# wiped warmup state 3x) only causes ONE recreate; subsequent ticks log
# "Debounced" and exit until the window passes, then deploy the latest
# tip in a single recreate. Override with DEPLOY_DEBOUNCE_SECONDS=0
# to disable.
DEBOUNCE_SECONDS="${DEPLOY_DEBOUNCE_SECONDS:-600}"
LAST_DEPLOY_FILE="${REPO_DIR}/.last-deploy"

cd "$REPO_DIR"

echo "$LOG_PREFIX $(date '+%Y-%m-%d %H:%M:%S') Checking for updates on $BRANCH..."

# Ensure we're on the deploy branch BEFORE checking. If a session left the
# working tree on a feature branch (e.g., after a hand-off from terminal
# Claude or local debugging), `git pull origin main` either no-ops or
# creates a wrong-branch merge — and every 5-min tick re-triggers the
# pull and a recreate, infinite-looping the container. The 2026-05-15
# session hit this for ~40 minutes before catching it. Stash-checkout-pop
# preserves any local edits (.env tweaks, etc.).
CURRENT_BRANCH=$(git rev-parse --abbrev-ref HEAD)
if [ "$CURRENT_BRANCH" != "$BRANCH" ]; then
    # Honor active Claude / human work-in-progress branches. Auto-switching
    # away from claude/* / wip/* / hotfix/* yanks the working tree out from
    # under a live session — that exact failure happened on 2026-05-15
    # at 20:35 (auto-deploy switched away from claude/crypto-enable,
    # so a crypto commit later landed on local main instead of the
    # feature branch). For everything else, restore the deploy invariant.
    case "$CURRENT_BRANCH" in
        claude/*|wip/*|hotfix/*)
            echo "$LOG_PREFIX On '$CURRENT_BRANCH' (active work branch) — skipping deploy this tick"
            exit 0
            ;;
    esac
    echo "$LOG_PREFIX On branch '$CURRENT_BRANCH', switching to '$BRANCH'"
    STASHED=false
    if ! git diff --quiet || ! git diff --cached --quiet; then
        git stash push -m "auto-deploy-$(date +%s)" --quiet && STASHED=true
    fi
    git checkout "$BRANCH" --quiet
    if [ "$STASHED" = true ]; then
        git stash pop --quiet || echo "$LOG_PREFIX Warning: stash pop conflict — manual review needed"
    fi
fi

# Fetch latest from remote
git fetch origin "$BRANCH" --quiet

# Check if local is BEHIND remote (not just unequal). On 2026-05-15 a
# session committed directly to local main (45318e4) while auto-deploy
# had checked the tree out from a feature branch. That local-only
# commit made local main perpetually a descendant of origin/main:
# LOCAL != REMOTE, but `git pull` was a no-op (nothing to merge), so
# the script recreated the container every 5 min for ~30 min while
# HEAD never moved. Counting commits in REMOTE-but-not-LOCAL handles
# all three topologies: equal (0 behind → exit), local-ahead
# (0 behind → exit, warn), and truly-behind (deploy).
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")
BEHIND=$(git rev-list --count "$LOCAL..$REMOTE")
AHEAD=$(git rev-list --count "$REMOTE..$LOCAL")
# What did we last actually deploy into the container? When commits are
# pushed FROM this VPS (a Claude/dev session running here), LOCAL=REMOTE
# the moment after push, so the BEHIND check below sees nothing new even
# though the running container is on an older image. Track the deployed
# SHA in the file body so we can detect that case. Backward-compatible:
# files written by the old script were empty (just used for mtime),
# which reads back as "" and we treat as "unknown / don't second-guess".
LAST_DEPLOYED_SHA=$(head -n1 "$LAST_DEPLOY_FILE" 2>/dev/null || true)

if [ "$BEHIND" -eq 0 ]; then
    if [ "$AHEAD" -gt 0 ]; then
        echo "$LOG_PREFIX Local is $AHEAD commit(s) AHEAD of origin/$BRANCH (HEAD=$(git rev-parse --short HEAD)) — local-only work not pushed. Skipping deploy."
        exit 0
    fi
    # In sync with origin. Normal case: nothing to do. Exception: this
    # VPS made the commits and pushed them itself, so origin and local
    # are equal but the running container hasn't been rebuilt yet.
    # Recognize this by comparing HEAD to the SHA we last deployed.
    if [ -n "$LAST_DEPLOYED_SHA" ] && [ "$LAST_DEPLOYED_SHA" != "$LOCAL" ]; then
        echo "$LOG_PREFIX HEAD=$(git rev-parse --short HEAD) differs from last-deployed=$(echo "$LAST_DEPLOYED_SHA" | cut -c1-7) — VPS-local commits already pushed; deploying."
        echo "$LOG_PREFIX Changes since last deploy:"
        git log --oneline "$LAST_DEPLOYED_SHA..$LOCAL" 2>/dev/null || true
        # fall through to debounce + build + recreate
    else
        echo "$LOG_PREFIX No changes detected. Bot is up to date."
        exit 0
    fi
else
    echo "$LOG_PREFIX Changes detected! ($BEHIND commit(s) behind, $AHEAD ahead)"
    echo "$LOG_PREFIX   Local:  $LOCAL"
    echo "$LOG_PREFIX   Remote: $REMOTE"
    echo "$LOG_PREFIX Changes:"
    git log --oneline "$LOCAL..$REMOTE"
fi

# Docs-only changes don't need a rebuild — the running container only
# cares about code, configs, and dependencies. A HANDOFF.md push that
# triggers a full container recreate drops in-memory bot state (live
# positions, scalp monitors, streaming subscriptions, IBKR connections)
# for no functional benefit. Skip if the only files changed since the
# last deploy are docs.
#
# Concrete cost observed 2026-05-18 03:35 UTC: two consecutive
# HANDOFF-only commits each triggered a rebuild + recreate, dropping
# 3 live crypto positions (AVAX/DOT/XRP) that became broker-side
# orphans on TradersPost.
DOC_ONLY=true
if [ -n "$LAST_DEPLOYED_SHA" ]; then
    DIFF_FILES=$(git diff --name-only "$LAST_DEPLOYED_SHA..HEAD" 2>/dev/null || echo "")
    if [ -z "$DIFF_FILES" ]; then
        DOC_ONLY=false  # Couldn't compute diff — fall through to deploy
    else
        while IFS= read -r f; do
            [ -z "$f" ] && continue
            case "$f" in
                *.md|README|LICENSE|CHANGELOG|HANDOFF*|docs/*)
                    : ;;  # doc file, keep DOC_ONLY=true
                *)
                    DOC_ONLY=false; break ;;
            esac
        done <<< "$DIFF_FILES"
    fi
else
    DOC_ONLY=false  # First-ever deploy — go through full path
fi
if [ "$DOC_ONLY" = true ]; then
    echo "$LOG_PREFIX Docs-only change since $(echo "$LAST_DEPLOYED_SHA" | cut -c1-7) — skipping rebuild, marking deployed."
    git rev-parse HEAD > "$LAST_DEPLOY_FILE"
    exit 0
fi

# Debounce: collapse rapid-fire commits into one recreate. Skip the
# pull too — if we pulled now but skipped the recreate, the container
# would run stale code AND the next tick would see "no changes" and
# never recreate.
if [ "$DEBOUNCE_SECONDS" -gt 0 ] && [ -f "$LAST_DEPLOY_FILE" ]; then
    LAST_DEPLOY_TS=$(stat -c %Y "$LAST_DEPLOY_FILE" 2>/dev/null || echo 0)
    NOW_TS=$(date +%s)
    AGE=$((NOW_TS - LAST_DEPLOY_TS))
    if [ "$AGE" -lt "$DEBOUNCE_SECONDS" ]; then
        WAIT=$((DEBOUNCE_SECONDS - AGE))
        echo "$LOG_PREFIX Debounced — last deploy was ${AGE}s ago (window ${DEBOUNCE_SECONDS}s). Will deploy in ~${WAIT}s."
        exit 0
    fi
fi

# Pull the changes (no-op when BEHIND=0; only matters in the
# remote-newer-than-local path)
git pull origin "$BRANCH" --quiet

# Always rebuild the image, even for code-only changes. The old script only
# rebuilt on Dockerfile / requirements.txt changes and used --force-recreate
# otherwise — but Python source is BAKED INTO the image at build time, so
# without --build the running container ran whatever code was in the image at
# its last build, NOT what was just pulled. The 2026-05-15 session saw
# multiple "Bot restarted successfully! New version: <sha>" lines while the
# container kept running stale code for hours. Docker's layer cache makes
# code-only rebuilds fast (~3-5s).
USE_SYSTEMD=false
if systemctl list-unit-files trading-bot.service >/dev/null 2>&1; then
    USE_SYSTEMD=true
fi

echo "$LOG_PREFIX Building and restarting trading-bot..."
docker compose build trading-bot --quiet
if [ "$USE_SYSTEMD" = true ]; then
    # Systemd-managed: bounce just the bot container via compose so we
    # don't take down IB Gateway (avoids the slow cold-start login).
    docker compose up -d --force-recreate trading-bot
else
    docker compose up -d --force-recreate trading-bot
fi

# Verify it's running
sleep 5
if docker compose ps trading-bot | grep -q "Up"; then
    echo "$LOG_PREFIX Bot restarted successfully!"
    echo "$LOG_PREFIX   New version: $(git rev-parse --short HEAD)"
    # Body = deployed SHA (HEAD ≠ deployed SHA check needs this); mtime
    # = debounce baseline.
    git rev-parse HEAD > "$LAST_DEPLOY_FILE"
else
    echo "$LOG_PREFIX ERROR: Bot failed to start! Check logs:"
    echo "$LOG_PREFIX   docker compose logs --tail 50 trading-bot"
    exit 1
fi
