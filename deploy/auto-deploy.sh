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

# Check if Dockerfile or requirements.txt changed (needs rebuild)
CHANGED_FILES=$(git diff --name-only "$LOCAL" "$REMOTE")
NEEDS_REBUILD=false

if echo "$CHANGED_FILES" | grep -qE "^(Dockerfile|requirements\.txt)$"; then
    NEEDS_REBUILD=true
    echo "$LOG_PREFIX Dockerfile or requirements.txt changed — full rebuild needed"
fi

# Restart the bot. Prefer systemctl if the unit is installed — that way
# the stack stays under systemd's control and the watchdog sees consistent state.
USE_SYSTEMD=false
if systemctl list-unit-files trading-bot.service >/dev/null 2>&1; then
    USE_SYSTEMD=true
fi

if [ "$NEEDS_REBUILD" = true ]; then
    echo "$LOG_PREFIX Rebuilding trading-bot image..."
    docker compose build trading-bot --quiet
    if [ "$USE_SYSTEMD" = true ]; then
        echo "$LOG_PREFIX Restarting via systemctl..."
        systemctl restart trading-bot
    else
        docker compose up -d trading-bot
    fi
else
    echo "$LOG_PREFIX Restarting trading-bot (no rebuild needed)..."
    if [ "$USE_SYSTEMD" = true ]; then
        # Systemd-managed: bounce just the bot container via compose so we
        # don't take down IB Gateway (avoids the slow cold-start login).
        docker compose up -d --force-recreate trading-bot
    else
        docker compose up -d --force-recreate trading-bot
    fi
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
