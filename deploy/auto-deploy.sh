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

# Check if local is behind remote
LOCAL=$(git rev-parse HEAD)
REMOTE=$(git rev-parse "origin/$BRANCH")

if [ "$LOCAL" = "$REMOTE" ]; then
    echo "$LOG_PREFIX No changes detected. Bot is up to date."
    exit 0
fi

echo "$LOG_PREFIX Changes detected!"
echo "$LOG_PREFIX   Local:  $LOCAL"
echo "$LOG_PREFIX   Remote: $REMOTE"

# Show what changed
echo "$LOG_PREFIX Changes:"
git log --oneline "$LOCAL..$REMOTE"

# Pull the changes
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
else
    echo "$LOG_PREFIX ERROR: Bot failed to start! Check logs:"
    echo "$LOG_PREFIX   docker compose logs --tail 50 trading-bot"
    exit 1
fi
