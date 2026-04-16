#!/bin/bash
# ===========================================================
# Trading Bot Watchdog
# ===========================================================
#
# Runs every 2 minutes via cron. Checks container + dashboard health and
# restarts the stack if anything is broken. Sends Discord alerts on
# restart actions and recovery — so the user knows immediately if
# buy/exit signals might be missed due to downtime.
#
# Checks (in order):
#   1. Docker daemon up
#   2. ib-gateway container running
#   3. trading-bot container running
#   4. Dashboard /health returns 200 AND engine.running == true
#   5. IB Gateway healthcheck is "healthy" (only during trading hours —
#      outside of those, IBKR maintenance can legitimately put it
#      "unhealthy" and restarting won't help)
#
# If any check fails → `docker compose up -d` + Discord alert.
# Rate-limited: at most one restart action per 10 min.
#
# Install:
#   */2 * * * * /opt/trading-bot/deploy/watchdog.sh >> /var/log/trading-bot-watchdog.log 2>&1
# ===========================================================

set -euo pipefail

REPO_DIR="${REPO_DIR:-/opt/trading-bot}"
COMPOSE="docker compose -f $REPO_DIR/docker-compose.yml"
STATE_FILE="/var/run/trading-bot-watchdog.state"
HEALTH_STATE_FILE="/var/run/trading-bot-watchdog.health"
COOLDOWN_SECS=600  # Don't restart more often than once per 10 min
HEALTH_URL="http://127.0.0.1:5000/health"

ts() { date '+%Y-%m-%d %H:%M:%S'; }
et_ts() { TZ=America/New_York date '+%H:%M ET'; }
log() { echo "[$(ts)] $*"; }

cd "$REPO_DIR" 2>/dev/null || { log "FATAL: $REPO_DIR not found"; exit 1; }

# Is IBKR reachable right now? IBKR does nightly server maintenance
# (~23:45–00:45 ET weekdays) and a longer weekend window. During those
# stretches IB Gateway legitimately can't connect; restarting just
# thrashes. Only act on ib-gateway health during active trading hours.
in_trading_window() {
    local dow hour
    dow=$(TZ=America/New_York date +%u)    # 1=Mon ... 7=Sun
    hour=$(TZ=America/New_York date +%H)   # 00..23 Eastern
    # Mon-Fri, 04:00 ≤ hour < 20:00 ET (pre-market 4am → post-market close 8pm)
    if [ "$dow" -le 5 ] && [ "$hour" -ge 4 ] && [ "$hour" -lt 20 ]; then
        return 0
    fi
    return 1
}

# --- Discord alerting (needs its own webhook read — the bot may be down) ---
# Parsing instead of sourcing the .env so a malformed value can't execute
# anything. Extract the first DISCORD_WEBHOOK_URL line, strip quotes.
_get_discord_webhook() {
    local env_file="$REPO_DIR/.env"
    [ -f "$env_file" ] || return 1
    grep -E '^DISCORD_WEBHOOK_URL=' "$env_file" | head -1 | cut -d= -f2- | tr -d '"' | tr -d "'"
}

discord_alert() {
    local msg="$1"
    local webhook
    webhook=$(_get_discord_webhook 2>/dev/null || true)
    [ -z "$webhook" ] && return 0

    # JSON-escape the message (newlines, quotes, backslashes)
    local escaped
    escaped=$(printf '%s' "$msg" | python3 -c 'import sys,json; print(json.dumps(sys.stdin.read()))' 2>/dev/null)
    [ -z "$escaped" ] && return 0

    local payload
    payload=$(printf '{"content":%s,"username":"AlgoBot Watchdog"}' "$escaped")
    curl -s -X POST -H "Content-Type: application/json" \
        -d "$payload" "$webhook" --max-time 10 >/dev/null 2>&1 || true
}

# --- Rate limit on restart actions ---
now=$(date +%s)
cooldown_active=false
if [ -f "$STATE_FILE" ]; then
    last_restart=$(cat "$STATE_FILE" 2>/dev/null || echo 0)
    if [ "$((now - last_restart))" -lt "$COOLDOWN_SECS" ]; then
        cooldown_active=true
    fi
fi

# --- Check 1: Docker daemon up? ---
if ! docker info >/dev/null 2>&1; then
    log "ALERT: Docker daemon not responding. Attempting to start..."
    if ! $cooldown_active; then
        discord_alert "🚨 **WATCHDOG**: Docker daemon is not responding on the Linode. Attempting to start it. ($(et_ts))"
        systemctl start docker || log "ERROR: systemctl start docker failed"
        echo "$now" > "$STATE_FILE"
    fi
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
if [ -z "$restart_reason" ]; then
    health_body=$(curl --silent --max-time 5 --fail "$HEALTH_URL" 2>/dev/null || echo "")
    if [ -z "$health_body" ]; then
        restart_reason="dashboard /health not responding"
    elif ! echo "$health_body" | grep -q '"running":\s*true'; then
        restart_reason="engine.running is false (bot loop stopped)"
    fi
fi

# --- Check 5: IB Gateway healthcheck status (only during trading hours) ---
if [ -z "$restart_reason" ]; then
    ibgw_health=$(docker inspect --format='{{.State.Health.Status}}' \
        "$($COMPOSE ps -q ib-gateway 2>/dev/null)" 2>/dev/null || echo "unknown")
    if [ "$ibgw_health" = "unhealthy" ]; then
        if in_trading_window; then
            restart_reason="ib-gateway container healthcheck reporting unhealthy"
        fi
        # Outside trading hours: silent skip. IBKR maintenance — not our problem.
    fi
fi

# --- Health-state transitions: alert on down→up and up→down ---
current_health=$(if [ -n "$restart_reason" ]; then echo "unhealthy"; else echo "healthy"; fi)
last_health=$(cat "$HEALTH_STATE_FILE" 2>/dev/null || echo "unknown")

# --- Take action if something is broken and we're not in cooldown ---
if [ -n "$restart_reason" ]; then
    if $cooldown_active; then
        # Already restarted recently — don't hammer. Just exit quietly.
        exit 0
    fi

    log "RESTART: $restart_reason"

    # Decide whether to alert: first detection, or return-to-broken after recovery.
    if [ "$last_health" != "unhealthy" ]; then
        discord_alert "🚨 **WATCHDOG ALERT** — Bot is DOWN at $(et_ts)
$restart_reason
Attempting auto-recovery now. Trades may be missed until resolved."
    fi

    log "Running: $COMPOSE up -d"
    if $COMPOSE up -d 2>&1 | sed "s/^/[$(ts)] compose: /"; then
        log "Stack started. Sleeping 15s then verifying..."
        sleep 15
        if $COMPOSE ps --status running --services | grep -Fxq trading-bot; then
            log "OK: trading-bot is running after restart"
            # Recovery alert only if we were previously unhealthy; this is the
            # "back online" confirmation the user wants to see.
            if [ "$last_health" = "unhealthy" ] || [ "$last_health" = "unknown" ]; then
                discord_alert "✅ **WATCHDOG OK** — Bot recovered at $(et_ts) after: $restart_reason"
            fi
            current_health=healthy
        else
            log "ERROR: trading-bot still not running after restart. Manual intervention needed."
            log "  Try: $COMPOSE logs --tail 100 trading-bot"
            discord_alert "⛔ **WATCHDOG CRITICAL** — Restart FAILED at $(et_ts)
$restart_reason
Automatic recovery did not succeed. Manual intervention required.
On the Linode: \`docker compose -f $REPO_DIR/docker-compose.yml logs --tail 100 trading-bot\`"
        fi
    else
        log "ERROR: $COMPOSE up -d failed"
        discord_alert "⛔ **WATCHDOG CRITICAL** — \`docker compose up -d\` FAILED at $(et_ts)
$restart_reason
Manual intervention required on the Linode."
    fi
    echo "$now" > "$STATE_FILE"
else
    # All healthy. If we were previously unhealthy (caught during cooldown,
    # or recovered between cron runs), emit the recovery alert now.
    if [ "$last_health" = "unhealthy" ]; then
        log "OK: bot returned to healthy state"
        discord_alert "✅ **WATCHDOG OK** — Bot is back to healthy state at $(et_ts)"
    fi
fi

# Persist current health state for the next cron run
echo "$current_health" > "$HEALTH_STATE_FILE"
