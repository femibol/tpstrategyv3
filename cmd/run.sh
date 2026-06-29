#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -25
echo
echo "=== verify close-path guard is live ==="
docker exec trading-bot-trading-bot-1 bash -c "grep -c '_sane_exit_price' /app/bot/engine.py 2>&1"
echo "(expect >= 3: 1 def + 2 call sites)"
echo
echo "=== JUP-USD record still corrected after deploy (data is bind-mounted)? ==="
python3 -c "
import json
trades = json.load(open('data/trade_history.json'))
bad = [t for t in trades if (t.get('symbol') or '').upper()=='JUP-USD' and (t.get('pnl') or 0) < -50]
print(f'JUP-USD records with pnl < -50: {len(bad)} (expect 0)')
"
echo
echo "=== account state after restart (drawdown should reflect correction) ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/status" > /tmp/st.json
python3 -c "
import json
d = json.load(open('/tmp/st.json'))
print(f'  balance=\${d.get(\"balance\"):,.2f}  total_return={d.get(\"total_return_pct\"):+.2f}%  drawdown={d.get(\"drawdown_pct\"):.2f}%  positions={d.get(\"positions\")}')
" 2>&1 || cat /tmp/st.json
