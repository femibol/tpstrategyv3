#!/bin/bash
cd /opt/trading-bot
SECRET=$(grep -E "^DASHBOARD_SECRET_KEY" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
echo "secret-set: $([ -n "$SECRET" ] && echo yes || echo no)"

echo ""
echo "=== bot's DELL position before close ==="
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
d = pos.get('DELL')
if d:
    print(f'  qty={d[\"quantity\"]}  entry=\${d[\"entry_price\"]:.2f}  current=\${d.get(\"current_price\",0):.2f}  unr={d.get(\"unrealized_pnl_pct\",0)*100:+.2f}%  stop=\${d.get(\"stop_loss\",0):.2f}')
else:
    print('  (no DELL position tracked)')
"

echo ""
echo "=== POST /api/control/close/DELL ==="
curl -s -m 30 -u admin:"$SECRET" -X POST http://localhost:5000/api/control/close/DELL
echo ""

echo ""
echo "=== bot's DELL position after close (re-check in 8s) ==="
sleep 8
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
d = pos.get('DELL')
if d:
    print(f'  STILL OPEN: qty={d[\"quantity\"]}  current=\${d.get(\"current_price\",0):.2f}')
else:
    print('  FLAT — DELL no longer in positions_state.json ✓')
"

echo ""
echo "=== docker logs tail — what happened ==="
docker logs trading-bot-trading-bot-1 --since 1m 2>&1 | grep -iE "DELL|API.*close|control.*close" | tail -10
