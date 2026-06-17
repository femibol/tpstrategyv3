#!/bin/bash
set +e
cd /opt/trading-bot
git fetch origin main --quiet 2>&1 | tail -1
git show origin/main:scripts/deploy-vps.sh > scripts/deploy-vps.sh
chmod +x scripts/deploy-vps.sh
scripts/deploy-vps.sh
echo "=== verify Cache-Control header on / ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
curl -s -m 10 -I -u "admin:$DASHBOARD_SECRET_KEY" http://localhost:5000/ 2>&1 | grep -iE "HTTP|cache-control|pragma|expires"
echo "=== via tailnet ==="
curl -s -m 12 -I -u "admin:$DASHBOARD_SECRET_KEY" https://trading-bot-vps.tail5db65d.ts.net/ 2>&1 | grep -iE "HTTP|cache-control|pragma|expires"
