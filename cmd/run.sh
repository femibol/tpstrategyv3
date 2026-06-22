#!/bin/bash
set +e
cd /opt/trading-bot

echo "=== ICCM/QXL — full log timeline today (why APPROVED but no fill?) ==="
for sym in ICCM QXL WKSP AXTX WDCX; do
  hits=$(docker logs --since 12h trading-bot-trading-bot-1 2>&1 | grep -c "\\b$sym\\b")
  echo "--- $sym ($hits log hits in last 12h) ---"
  docker logs --since 12h trading-bot-trading-bot-1 2>&1 | grep -E "\\b$sym\\b" | head -20
  echo
done

echo "=== JUP-USD — what happened? full timeline today ==="
docker logs --since 24h trading-bot-trading-bot-1 2>&1 | grep "\\bJUP-USD\\b" | tail -25

echo
echo "=== JUP-USD live price right now ==="
curl -s "https://api.coinbase.com/v2/prices/JUP-USD/spot" 2>&1 | head -3
echo
echo "=== JUP entry context (from trade_history.json) ==="
python3 -c "
import json
trades = json.load(open('data/trade_history.json'))
jup = [t for t in trades if (t.get('symbol') or '').upper() == 'JUP-USD']
print(f'total JUP-USD historical trades: {len(jup)}')
for t in jup[-10:]:
    print(f'  {t.get(\"exit_time\",\"?\")[:19]}  entry=\${t.get(\"entry_price\",0):.4f}  exit=\${t.get(\"exit_price\",0):.4f}  qty={t.get(\"quantity\")}  pnl=\${t.get(\"pnl\",0):+.2f}  reason={t.get(\"reason\")}'  )
"

echo
echo "=== open positions file (data/open_positions.json) — what's stored? ==="
docker exec trading-bot-trading-bot-1 cat /app/data/open_positions.json 2>&1 | python3 -m json.tool 2>&1 | head -60
