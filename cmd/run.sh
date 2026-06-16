#!/bin/bash
set +e
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
AUTH=$(printf 'admin:%s' "$DASHBOARD_SECRET_KEY" | base64 -w0)

echo "=== container ==="
docker ps --filter name=trading-bot-trading-bot --format '{{.Names}} | {{.Status}}'

echo "=== /health ==="
curl -s -m 5 http://localhost:5000/health

echo "=== /api/status (key fields, with auth) ==="
curl -s -m 12 -H "Authorization: Basic $AUTH" http://localhost:5000/api/status | python3 -c '
import json,sys
try: d=json.load(sys.stdin)
except Exception as e: print("PARSE FAIL:",e); sys.exit(0)
for k in ["running","paused","balance","positions","total_trades","broker_connected","execution_broker"]:
    print(f"{k}: {d.get(k)}")
'

echo "=== dashboard HTML — UI overhaul markers present? ==="
HTML=$(curl -sS -m 10 -H "Authorization: Basic $AUTH" http://localhost:5000/)
echo -n "stats-hero present: "; echo "$HTML" | grep -q "stats-hero" && echo YES || echo NO
echo -n "stat-card hero present: "; echo "$HTML" | grep -q "stat-card hero" && echo YES || echo NO
echo -n "stats-tertiary present: "; echo "$HTML" | grep -q "stats-tertiary" && echo YES || echo NO
echo -n "_todaysStatsFromTrades present: "; echo "$HTML" | grep -q "_todaysStatsFromTrades" && echo YES || echo NO

echo "=== last 30 log lines (look for engine bring-up signatures) ==="
docker logs --tail 60 trading-bot-trading-bot-1 2>&1 | grep -E "TRADE HISTORY|PERF STATS|Loaded strategy|IBKR connect|engine ready|Strategy loaded|started|error|Error" | tail -20

echo "=== any ERROR/CRITICAL in last 60s ==="
docker logs --since 60s trading-bot-trading-bot-1 2>&1 | grep -E "ERROR|CRITICAL|Traceback" | tail -15
