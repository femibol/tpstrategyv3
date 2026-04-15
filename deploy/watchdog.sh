#!/bin/bash
# ===========================================================
# Trading Bot Watchdog
# ===========================================================
#
# Runs every 2 minutes via cron. Checks four things in order and
# escalates only if something is actually wrong:
#
#   1. ib-gateway container is running
#   2. trading-bot container is running
#   3. Dashboard /health endpoint returns 200 AND reports running=true
#   4. IB Gateway healthcheck is healthy (not unhealthy / starting)
#
# If any check fails, the action is always the same: `docker compose up -d`.
# That's a no-op when everything is fine and a full recovery when not.
#
# Rate-limiting: if we restart the stack, we don't restart it again for
# 10 minutes — gives IB Gateway time to log in.
#
# Install:
#   */2 * * * * /opt/trading-bot/deploy/watchdog.sh >> /var/log/trading-bot-watchdog.log 2>&1
# ===========================================================

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/trading-bot}"
COMPOSE="docker compose -f $REPO_DIR/docker-compose.yml"
STATE_FILE="/var/run/trading-bot-watchdog.state"
COOLDOWN_SECS=600  # Don't restart more often than once per 10 min
HEALTH_URL="http://127.0.0.1:5000/health"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
log() { echo "[$(ts)] $*"; }

cd "$REPO_DIR" 2>/dev/null || { log "FATAL: $REPO_DIR not found"; exit 1; }

# --- Rate limit ---
now=$(date +%s)
if [ -f "$STATE_FILE" ]; then
    last_restart=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
    if [ "$((now - last_restart))" -lt "$COOLDOWN_SECS" ]; then
        remaining=$((COOLDOWN_SECS - (now - last_restart)))
        # Silent exit during cooldown — nothing to say unless there's a problem we can't act on.
        exit 0
    fi
fi

# --- Check 1: Docker daemon up? ---
if ! docker info >/dev/null 2>&1; then
    log "ALERT: Docker daemon not responding. Attempting to start..."
    systemctl start docker || log "ERROR: systemctl start docker failed"
    echo "$now" > "$STATE_FILE"
    exit 1
fi

# --- Check 2+3: Containers running? ---
restart_reason=""
for svc in ib-gateway trading-bot; do
    state=$($COMPOSE ps --status running --services 2>/dev/null | grep -Fx "$svc" || true)
    if [ -z "$state" ]; then
        restart_reason="$svc not running"
        break
    fi
done

# --- Check 4: Dashboard health responds? ---
# Only check if containers appear up (otherwise we already know we need to restart).
if [ -z "$restart_reason" ]; then
    health_body=$(curl --silent --max-time 5 --fail "$HEALTH_URL" 2>/dev/null || echo "")
    if [ -z "$health_body" ]; then
        restart_reason="dashboard /health not responding"
    elif ! echo "$health_body" | grep -q '"running":\s*true'; then
        restart_reason="engine.running is false (bot loop stopped)"
    fi
fi

# --- Check 5: IB Gateway healthcheck status ---
if [ -z "$restart_reason" ]; then
    ibgw_health=$(docker inspect --format='{{.State.Health.Status}}' \
        "$($COMPOSE ps -q ib-gateway 2>/dev/null)" 2>/dev/null || echo "unknown")
    # "unhealthy" = failed checks. "starting" is fine (cold boot). We don't act on "starting".
    if [ "$ibgw_health" = "unhealthy" ]; then
        restart_reason="ib-gateway container healthcheck reporting unhealthy"
    fi
fi

# --- Take action if needed ---
if [ -n "$restart_reason" ]; then
    log "RESTART: $restart_reason"
    log "Running: $COMPOSE up -d"
    if $COMPOSE up -d 2>&1 | sed "s/^/[$(ts)] compose: /"; then
        log "Stack started. Sleeping 15s then verifying..."
        sleep 15
        if $COMPOSE ps --status running --services | grep -Fxq trading-bot; then
            log "OK: trading-bot is running after restart"
        else
            log "ERROR: trading-bot still not running after restart. Manual intervention needed."
            log "  Try: $COMPOSE logs --tail 100 trading-bot"
        fi
    else
        log "ERROR: $COMPOSE up -d failed"
    fi
    echo "$now" > "$STATE_FILE"
fi
# Silent when everything is fine — keeps the log readable.
