#!/bin/bash
set +e
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
AUTH=$(printf 'admin:%s' "$DASHBOARD_SECRET_KEY" | base64 -w0)

echo "=== data/trade_history.json on disk ==="
ls -la /opt/trading-bot/data/trade_history.json 2>&1
echo "count:"
python3 -c "import json; print(len(json.load(open('/opt/trading-bot/data/trade_history.json'))))" 2>&1

echo "=== boot log: TRADE HISTORY + PERF STATS ==="
docker logs trading-bot-trading-bot-1 2>&1 | grep -E "TRADE HISTORY|PERF STATS" | tail -6

echo "=== /api/status total_trades ==="
curl -s -m 12 -H "Authorization: Basic $AUTH" http://localhost:5000/api/status | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("total_trades:", d.get("total_trades"))
print("running:", d.get("running"))
'
echo "=== /api/performance ==="
curl -s -m 12 -H "Authorization: Basic $AUTH" http://localhost:5000/api/performance | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("total_trades:", d.get("total_trades"))
print("win_rate:", d.get("win_rate"))
print("net_pnl:", d.get("net_pnl"))
'
echo "=== /api/trades count ==="
curl -s -m 12 -H "Authorization: Basic $AUTH" http://localhost:5000/api/trades | python3 -c '
import json,sys
d=json.load(sys.stdin)
print("count:", len(d))
'
