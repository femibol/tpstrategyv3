#!/bin/bash
set +e
cd /opt/trading-bot
git fetch origin main --quiet 2>&1 | tail -1
git show origin/main:scripts/deploy-vps.sh > scripts/deploy-vps.sh
chmod +x scripts/deploy-vps.sh
scripts/deploy-vps.sh
echo "=== verify /api/status is now fast ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
AUTH=$(printf 'admin:%s' "$DASHBOARD_SECRET_KEY" | base64 -w0)
# Hit it twice; first warms the cache (still slow), second should be fast
echo -n "first call:  "
curl -s -m 30 -o /dev/null -w "HTTP %{http_code} in %{time_total}s\n" -H "Authorization: Basic $AUTH" http://localhost:5000/api/status
echo -n "second call: "
curl -s -m 30 -o /dev/null -w "HTTP %{http_code} in %{time_total}s\n" -H "Authorization: Basic $AUTH" http://localhost:5000/api/status
echo -n "scanner:     "
curl -s -m 10 -o /dev/null -w "HTTP %{http_code} in %{time_total}s\n" -H "Authorization: Basic $AUTH" http://localhost:5000/api/scanner
ls -la data/trade_history.json | awk '{print "trade_history.json: " $5 " bytes"}'
