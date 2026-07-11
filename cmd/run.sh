#!/bin/bash
set +e
cd /opt/trading-bot
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"

echo "=== what YOUR DASHBOARD shows for the ALGO position right now ==="
curl -s -m 10 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'positions: {len(d)}')
for p in d:
    print(f'  {p.get(\"symbol\")}: entry=\${p.get(\"entry_price\",0):.4f}  now=\${p.get(\"current_price\",0):.4f}  pnl=\${p.get(\"pnl_dollars\",0):+.2f} ({p.get(\"pnl_pct\",0):+.2f}%)  price_source={p.get(\"price_source\")}')
" 2>&1

echo
echo "=== reference: Coinbase spot for real ALGO ==="
curl -s -m 8 "https://api.coinbase.com/v2/prices/ALGO-USD/spot" 2>&1 | head -2

echo
echo "=== bot's internal mark (from log, last price refs) ==="
docker logs --since 2h trading-bot-trading-bot-1 2>&1 | grep -oE "ALGO-USD[^|]*\$0\.[0-9]+" | tail -3
