#!/bin/bash
cd /opt/trading-bot
echo "=== bot's known positions right now ==="
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
print(f'count: {len(pos)}')
for sym, p in pos.items():
    print(f\"  {sym:<10s} {p.get('strategy','?'):<18s} qty={p.get('quantity',0):.4g}  entry=\${p.get('entry_price',0):.4f}  unr={p.get('unrealized_pnl_pct',0)*100:+5.2f}%\")
"

echo ""
echo "=== bot's recent activity on RKLB/SMCI (last 30h docker logs) ==="
docker logs trading-bot-trading-bot-1 --since 30h 2>&1 | grep -iE "RKLB|SMCI" | tail -20

echo ""
echo "=== minutes to market open (NY) ==="
python3 -c "
from datetime import datetime
import zoneinfo
ny = datetime.now(zoneinfo.ZoneInfo('America/New_York'))
open_t = ny.replace(hour=9, minute=30, second=0, microsecond=0)
mins = int((open_t - ny).total_seconds() / 60)
print(f'NY time: {ny.strftime(\"%H:%M:%S\")}  → open in {mins}m')
"
