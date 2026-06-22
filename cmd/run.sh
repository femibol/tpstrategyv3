#!/bin/bash
set +e
cd /opt/trading-bot

echo "=== WDCX full timeline today — was it closed? ==="
docker logs --since 12h trading-bot-trading-bot-1 2>&1 | grep "\\bWDCX\\b" | tail -40

echo
echo "=== WDCX in trade_history? ==="
python3 -c "
import json
trades = json.load(open('data/trade_history.json'))
wdcx = [t for t in trades if (t.get('symbol') or '').upper() == 'WDCX']
print(f'WDCX trades: {len(wdcx)}')
for t in wdcx[-5:]:
    print(f'  {t.get(\"exit_time\",\"?\")[:19]}  entry=\${t.get(\"entry_price\",0):.4f}  exit=\${t.get(\"exit_price\",0):.4f}  qty={t.get(\"quantity\")}  pnl=\${t.get(\"pnl\",0):+.2f}  reason={t.get(\"reason\")}'  )
"

echo
echo "=== open_positions.json contents ==="
docker exec trading-bot-trading-bot-1 ls -la /app/data/open_positions.json 2>&1
docker exec trading-bot-trading-bot-1 bash -c "head -c 2000 /app/data/open_positions.json 2>&1 || echo 'cannot read file'"

echo
echo "=== JUP-USD live price (multiple sources) ==="
echo "Coinbase JUP-USD:"
curl -s "https://api.coinbase.com/v2/prices/JUP-USD/spot" 2>&1 | head -3
echo
echo "Coinbase JUP-USDT:"
curl -s "https://api.coinbase.com/v2/prices/JUP-USDT/spot" 2>&1 | head -3
echo
echo "CoinGecko JUP (Jupiter):"
curl -s "https://api.coingecko.com/api/v3/simple/price?ids=jupiter-exchange-solana&vs_currencies=usd" 2>&1 | head -3
echo

echo "=== bot internal price for JUP-USD ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
for p in d:
    if 'JUP' in p.get('symbol',''):
        print(f'  bot.current_price = \${p.get(\"current_price\")}')
        print(f'  bot.entry_price   = \${p.get(\"entry_price\")}')
        print(f'  bot.pnl_dollars   = \${p.get(\"pnl_dollars\")}')
        print(f'  bot.pnl_pct       = {p.get(\"pnl_pct\")}%')
        print(f'  bot.price_source  = {p.get(\"price_source\")}')
"

echo
echo "=== count of QUALITY GATE SKIP today ==="
docker logs --since 12h trading-bot-trading-bot-1 2>&1 | grep -c "QUALITY GATE SKIP"
echo "  (lines blocked by quality gates today)"
echo
echo "=== top quality-gate-skip symbols today ==="
docker logs --since 12h trading-bot-trading-bot-1 2>&1 | grep "QUALITY GATE SKIP" | grep -oE "SKIP: [A-Z]+" | sort | uniq -c | sort -rn | head -10
