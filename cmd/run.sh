#!/bin/bash
set +e
cd /opt/trading-bot

echo "=== fetching claude/live-state for backup ==="
git fetch origin claude/live-state -q 2>&1 | tail -2

echo "=== restoring trade_history.json from live-state snapshot ==="
git show origin/claude/live-state:data/trade_history.json > data/trade_history.json 2>/tmp/restore.err
echo "size: $(stat -c%s data/trade_history.json 2>/dev/null) bytes"
echo "count: $(python3 -c 'import json; print(len(json.load(open("data/trade_history.json"))))' 2>&1)"

echo "=== restoring signal_log.json from live-state snapshot ==="
git show origin/claude/live-state:data/signal_log.json > data/signal_log.json 2>/tmp/restore.err
echo "size: $(stat -c%s data/signal_log.json 2>/dev/null) bytes"

echo "=== restart container so trade_history is reloaded ==="
docker restart trading-bot-trading-bot-1
sleep 4

echo "=== wait for /health ==="
for i in $(seq 1 20); do
  if curl -s -m 3 http://localhost:5000/health | grep -q '"status":"ok"'; then
    echo "ready after $((i*3))s"; break
  fi
  sleep 3
done

echo "=== verify trade history loaded ==="
docker logs --since 90s trading-bot-trading-bot-1 2>&1 | grep -E "TRADE HISTORY|PERF STATS" | head -5

echo "=== check via API ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
AUTH=$(printf 'admin:%s' "$DASHBOARD_SECRET_KEY" | base64 -w0)
curl -s -m 12 -H "Authorization: Basic $AUTH" http://localhost:5000/api/status | python3 -c '
import json,sys
d=json.load(sys.stdin)
print(f"total_trades: {d.get(\"total_trades\")}")
print(f"running: {d.get(\"running\")}")
'
