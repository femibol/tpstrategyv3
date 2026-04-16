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

# --- 2FA early warning ---
# IBKR forces re-authentication periodically (weekly + after specific events).
# When it does, IB Gateway can't log in until you tap "Approve" on the IBKR
# mobile app. Watchdog restarts won't fix this — only the user can. So if
# Gateway has been unhealthy for >30 min during trading hours, post a
# distinct alert telling the user to check their phone.
IBGW_UNHEALTHY_SINCE_FILE="/var/run/trading-bot-watchdog.ibgw-unhealthy-since"
IBGW_2FA_ALERTED_FILE="/var/run/trading-bot-watchdog.ibgw-2fa-alerted"
TWO_FA_THRESHOLD_SECS=1800  # 30 min

if [ "${ibgw_health:-unknown}" = "unhealthy" ] && in_trading_window; then
    if [ ! -f "$IBGW_UNHEALTHY_SINCE_FILE" ]; then
        echo "$now" > "$IBGW_UNHEALTHY_SINCE_FILE"
    fi
    unhealthy_since=$(cat "$IBGW_UNHEALTHY_SINCE_FILE" 2>/dev/null || echo "$now")
    unhealthy_duration=$((now - unhealthy_since))
    if [ "$unhealthy_duration" -ge "$TWO_FA_THRESHOLD_SECS" ] && [ ! -f "$IBGW_2FA_ALERTED_FILE" ]; then
        log "ALERT: IB Gateway unhealthy for ${unhealthy_duration}s during trading hours — likely 2FA"
        discord_alert "🔐 **CHECK YOUR PHONE** — IB Gateway has been unable to connect to IBKR for $((unhealthy_duration / 60)) minutes during trading hours at $(et_ts).
This is almost always an IBKR 2FA prompt. Open the IBKR mobile app and approve the login.
Auto-recovery cannot fix this — only you can. Bot is NOT trading until resolved."
        echo "$now" > "$IBGW_2FA_ALERTED_FILE"
    fi
else
    # Healthy or off-hours — clear the streak so the next outage starts fresh
    rm -f "$IBGW_UNHEALTHY_SINCE_FILE" "$IBGW_2FA_ALERTED_FILE"
fi

# --- Outage dedup ---
# An "outage" is a continuous stretch where restart_reason is set. We alert
# ONCE at the start, then stay quiet until the outage clears. The previous
# logic confused itself by marking current_health="healthy" whenever the
# trading-bot container came back up after a restart — even if ib-gateway
# was still unhealthy. So when the cooldown expired, it thought it was a
# brand new outage and re-alerted every 10 min.
OUTAGE_ALERTED_FILE="/var/run/trading-bot-watchdog.outage-alerted"

if [ -n "$restart_reason" ]; then
    if $cooldown_active; then
        # Already restarted recently — don't hammer. Just exit quietly.
        exit 0
    fi

    log "RESTART: $restart_reason"

    # Alert once per outage. Re-arms only after a clean recovery.
    if [ ! -f "$OUTAGE_ALERTED_FILE" ]; then
        discord_alert "🚨 **WATCHDOG ALERT** — Bot is DOWN at $(et_ts)
$restart_reason
Attempting auto-recovery now. Trades may be missed until resolved."
        echo "$now" > "$OUTAGE_ALERTED_FILE"
    fi

    log "Running: $COMPOSE up -d"
    if $COMPOSE up -d 2>&1 | sed "s/^/[$(ts)] compose: /"; then
        log "Stack started. Sleeping 15s then verifying..."
        sleep 15
        if ! $COMPOSE ps --status running --services | grep -Fxq trading-bot; then
            log "ERROR: trading-bot still not running after restart. Manual intervention needed."
            discord_alert "⛔ **WATCHDOG CRITICAL** — Restart FAILED at $(et_ts)
$restart_reason
Automatic recovery did not succeed. Manual intervention required.
On the Linode: \`docker compose -f $REPO_DIR/docker-compose.yml logs --tail 100 trading-bot\`"
        else
            log "OK: trading-bot is running after restart (recovery alert deferred until full health confirmed)"
        fi
    else
        log "ERROR: $COMPOSE up -d failed"
        discord_alert "⛔ **WATCHDOG CRITICAL** — \`docker compose up -d\` FAILED at $(et_ts)
$restart_reason
Manual intervention required on the Linode."
    fi
    echo "$now" > "$STATE_FILE"
else
    # restart_reason is empty → we believe we're fully healthy this run.
    # If we previously alerted on an outage, NOW is the moment to send the
    # recovery message and re-arm dedup for next time.
    if [ -f "$OUTAGE_ALERTED_FILE" ]; then
        log "OK: bot returned to healthy state — clearing outage flag"
        discord_alert "✅ **WATCHDOG OK** — Bot is back to healthy state at $(et_ts)"
        rm -f "$OUTAGE_ALERTED_FILE"
    fi
fi

# (HEALTH_STATE_FILE no longer used — outage flag replaces it)
rm -f "$HEALTH_STATE_FILE" 2>/dev/null || true
