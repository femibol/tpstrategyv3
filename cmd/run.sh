#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -40
echo "=== verify update() split is live ==="
echo "(checks the inline JS in the served dashboard contains both renderCritical + renderSecondary)"
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
HTML=$(curl -s -m 15 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/")
echo "renderCritical count: $(echo "$HTML" | grep -c 'renderCritical')"
echo "renderSecondary count: $(echo "$HTML" | grep -c 'renderSecondary')"
echo "(expect both >= 2 — one def, one call)"
