#!/bin/bash
# ===========================================================
# One-shot Kelly-sizing review
# ===========================================================
# Scheduled by a cron entry that this script removes on its first run, so
# it fires exactly once. Runs `claude -p` against the LIVE trade data on
# the VPS (a remote /schedule routine can't — data/ is gitignored) and
# posts the verdict to Discord.
#
# Installed 2026-05-21 to confirm the per-strategy Kelly ramp
# (kelly_max_mult 1.0 -> 2.0, deployed in b5cbb0e) is sizing correctly.
# ===========================================================
set -uo pipefail

REPO=/opt/trading-bot
LOG=/var/log/kelly-review.log
export HOME=/root
export PATH=/root/.local/bin:/usr/local/bin:/usr/bin:/bin

# One-shot guard: drop this script's own cron line before doing anything,
# so a crash or hang can never make it repeat.
( crontab -l 2>/dev/null | grep -v 'kelly-review.sh' ) | crontab - 2>/dev/null || true

cd "$REPO" || exit 1
echo "[kelly-review] $(date -u '+%Y-%m-%dT%H:%M:%SZ') starting" >> "$LOG"

PROMPT='You are reviewing an automated trading bot on its own VPS. Today is 2026-05-22. On 2026-05-21 a sizing change was deployed: per-strategy Kelly with kelly_max_mult=2.0, so a strategy with a proven edge (mean_reversion, ~61% win rate) sizes up to ~2% risk-per-trade instead of the previous blended-Kelly floor of ~0.25%. Confirm it is working.

Do this:
1. Read data/trade_history.json. Filter trades whose exit_time starts with 2026-05-22. Report total count, net P&L and win rate, broken down by strategy and by crypto vs equity (crypto symbols end in -USD/-USDT).
2. For mean_reversion trades today, estimate risk-per-trade as (entry_price - initial_stop) * quantity as a percent of a ~$24,000 account. Compare to the ~0.25% pre-ramp baseline — did sizing actually increase toward ~2%?
3. Grep logs/trading.log for ADAPTIVE SIZING lines dated 2026-05-22 and report the per-strategy Kelly multipliers shown (e.g. Kelly[mean_reversion] 2.00x).
4. Verdict: is the larger sizing netting positive, or causing outsized losses/drawdown? Note the largest single-trade loss today vs prior days, and flag any concern (a losing strategy still sized large, or drawdown approaching the -2/-3.5/-5% circuit-breaker tiers).

Write a concise Discord-friendly report: a clear one-line verdict first, then short paragraphs. Do not edit any files.'

# --allowed-tools pre-approves exactly these read-only-review tools so the
# run is fully non-interactive. (bypassPermissions is blocked for root.)
REVIEW=$(claude -p "$PROMPT" \
    --model sonnet \
    --allowed-tools "Bash Read Grep Glob" \
    --max-budget-usd 5 2>>"$LOG")

if [ -z "${REVIEW// }" ]; then
    REVIEW="Kelly-sizing review (2026-05-22): claude -p produced no output. Check $LOG on the VPS."
fi

echo "[kelly-review] review complete; posting to Discord" >> "$LOG"
printf '%s\n' "$REVIEW" >> "$LOG"

TMP=$(mktemp)
printf '%s' "$REVIEW" > "$TMP"

python3 - "$TMP" <<'PYEOF' >> "$LOG" 2>&1
import sys, json, time, urllib.request

with open('/opt/trading-bot/.env') as f:
    url = ''
    for line in f:
        if line.startswith('DISCORD_WEBHOOK_URL='):
            url = line.split('=', 1)[1].strip()
            break
if not url:
    print('[kelly-review] no DISCORD_WEBHOOK_URL — skipping post')
    sys.exit(0)

text = '\U0001F4CA **Kelly-Sizing Review — 2026-05-22**\n\n' + open(sys.argv[1]).read()
chunks = [text[i:i+1900] for i in range(0, len(text), 1900)] or ['(empty)']
for c in chunks:
    req = urllib.request.Request(
        url, data=json.dumps({'content': c}).encode(),
        headers={'Content-Type': 'application/json'})
    try:
        urllib.request.urlopen(req, timeout=20)
    except Exception as e:
        print(f'[kelly-review] Discord post failed: {e}')
    time.sleep(1)
print('[kelly-review] posted')
PYEOF

rm -f "$TMP"
echo "[kelly-review] $(date -u '+%Y-%m-%dT%H:%M:%SZ') done" >> "$LOG"
