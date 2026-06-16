#!/bin/bash
set +e
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
echo "key_len: ${#DASHBOARD_SECRET_KEY}"
echo "key startswith: ${DASHBOARD_SECRET_KEY:0:6}"
AUTH=$(printf 'admin:%s' "$DASHBOARD_SECRET_KEY" | base64 -w0)

echo "=== /api/status raw response (verbose) ==="
curl -sS -m 12 -i -H "Authorization: Basic $AUTH" http://localhost:5000/api/status 2>&1 | head -25
echo "--- raw body ---"
curl -sS -m 12 -H "Authorization: Basic $AUTH" http://localhost:5000/api/status 2>&1 | head -c 800
echo
echo "=== now via tailnet URL ==="
curl -sS -m 15 -o /dev/null -w "HTTP %{http_code}\n" -u "admin:$DASHBOARD_SECRET_KEY" https://trading-bot-vps.tail5db65d.ts.net/api/status
