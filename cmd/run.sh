#!/bin/bash
cd /opt/trading-bot

echo "=== ALL DELL/PLTA/MARA trades in the 500-trade buffer (any date) ==="
python3 -c "
import json
with open('data/trade_history.json') as f:
    trades = json.load(f)
watch = {'DELL','PLTA','MARA','PLTR','AAPL','TSLA'}
hits = [t for t in trades if t.get('symbol') in watch]
print(f'matches: {len(hits)}')
for t in hits[-20:]:
    print(f\"  {t.get('exit_time','?')[:19]}  {t.get('symbol','?'):<6s} {t.get('strategy','?'):<18s} {t.get('reason','?'):<22s} pnl=\${t.get('pnl',0):+8.2f}\")
"

echo ""
echo "=== EOD close events in docker logs since Fri close ==="
docker logs trading-bot-trading-bot-1 --since 60h 2>&1 | grep -iE "EOD|END.OF.DAY|eod_close|PRE.EOD" | tail -10

echo ""
echo "=== TradersPost SUBMITTED events for DELL/PLTA/MARA since Friday ==="
docker logs trading-bot-trading-bot-1 --since 60h 2>&1 | grep -iE "DELL|PLTA|MARA" | grep -iE "SUBMITTED|EXIT|CLOSE" | tail -20

echo ""
echo "=== Mirror reconcile output (orphans?) ==="
SECRET=$(grep -E "^DASHBOARD_SECRET_KEY" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
curl -s -m 10 -u admin:"$SECRET" -X POST http://localhost:5000/api/reconcile/mirror/run

echo ""
echo "=== Search code for OTHER paths that set trailing_stop directly ==="
grep -n 'pos\["trailing_stop"\] = ' /opt/trading-bot/bot/engine.py | head -10
