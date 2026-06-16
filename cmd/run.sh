#!/bin/bash
set +e
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
AUTH=$(printf 'admin:%s' "$DASHBOARD_SECRET_KEY" | base64 -w0)

# Time each endpoint that update() calls. Anything > 2s is suspect;
# > 10s is broken. Promise.all waits for slowest, so one bad endpoint
# stalls the whole UI refresh.
for ep in status positions trades notifications daily equity scanner analysis watchlist performance tips rvol suggestions movers runners trades/summary strategies/activity; do
  printf "%-25s " "/api/$ep"
  out=$(curl -s -m 30 -o /dev/null -w "%{http_code} %{time_total}s" -H "Authorization: Basic $AUTH" "http://localhost:5000/api/$ep" 2>&1)
  echo "$out"
done

echo "=== check container CPU usage ==="
docker stats --no-stream trading-bot-trading-bot-1 --format 'CPU={{.CPUPerc}} MEM={{.MemUsage}}'
