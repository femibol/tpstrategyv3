#!/bin/bash
set +e
cd /opt/trading-bot
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"

echo "=== 1. close ALGO-USD via dashboard control (clean flatten before paper reset) ==="
curl -s -m 20 -X POST -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/control/close/ALGO-USD" 2>&1 | head -3
sleep 8
echo
echo "=== 2. positions after close ==="
curl -s -m 10 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'open positions: {len(d)} (expect 0)')
for p in d: print(f'  still open: {p.get(\"symbol\")}')"
echo
echo "=== 3. how did the trade RECORD? (guards should book real ~\$4, never \$21K) ==="
python3 -c "
import json
t = json.load(open('data/trade_history.json'))
for x in t[-3:]:
    print(f'  {x.get(\"exit_time\",\"?\")[:19]}  {x.get(\"symbol\")}  pnl=\${x.get(\"pnl\",0):+.2f}  exit=\${x.get(\"exit_price\",0):.4f}  reason={x.get(\"reason\")}')"
echo
echo "=== 4. close log lines ==="
docker logs --since 5m trading-bot-trading-bot-1 2>&1 | grep -E "ALGO|CLOSED|ANTI-COLLISION" | tail -6
