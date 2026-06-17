#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -30
echo "=== verify render guards are live ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
HTML=$(curl -s -m 15 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/")
echo "_renderBlock count: $(echo "$HTML" | grep -c '_renderBlock')"
echo "renderErrorBox count: $(echo "$HTML" | grep -c 'renderErrorBox')"
echo "tvLink-guard present: $(echo "$HTML" | grep -c 'typeof symbol')"
echo "positions.length header fallback: $(echo "$HTML" | grep -c 'positions.length != null')"
echo "(expect _renderBlock >= 8, renderErrorBox >= 1, guards == 1, length-fallback == 1)"
