#!/bin/bash
cd /opt/trading-bot

echo "=== current DELL state ==="
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
d = pos.get('DELL')
print(f'  qty={d[\"quantity\"]} entry=\${d[\"entry_price\"]:.2f} current=\${d.get(\"current_price\",0):.2f} unr={d.get(\"unrealized_pnl_pct\",0)*100:+.2f}%' if d else '  no DELL')
"

echo ""
echo "=== last 3 min of bot logs filtered for close/cancel/DELL/error ==="
docker logs trading-bot-trading-bot-1 --since 3m 2>&1 | grep -iE "DELL|close|cancel|REJECTED|ERROR|control|timeout" | tail -25

echo ""
echo "=== verify the endpoint exists and what it returns ==="
SECRET=$(grep -E "^DASHBOARD_SECRET_KEY" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
curl -s -m 10 -u admin:"$SECRET" -X POST -w "\nHTTP %{http_code}\n" http://localhost:5000/api/control/close/DELL

echo ""
echo "=== try the close again with the IBKR worker possibly stuck — give it 20s this time ==="
curl -s -m 30 -u admin:"$SECRET" -X POST -w "\nHTTP %{http_code} time=%{time_total}s\n" http://localhost:5000/api/control/close/DELL

echo ""
echo "=== positions_state after retry ==="
sleep 5
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
print(f'  {list(pos.keys())}')"
