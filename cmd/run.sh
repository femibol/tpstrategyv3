#!/bin/bash
set +e
cd /opt/trading-bot
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"

echo "=== /api/positions — full response ==="
curl -s -m 10 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'count: {len(d)}')
print()
for p in d:
    sym = p.get('symbol','?')
    is_crypto = any(sfx in sym.upper() for sfx in ('-USD','-USDT','-BTC','-ETH'))
    label = 'CRYPTO' if is_crypto else 'EQUITY'
    print(f'  [{label}]  {sym:10s}  qty={p.get(\"quantity\")}  entry=\${p.get(\"entry_price\",0):.4f}  now=\${p.get(\"current_price\",0):.4f}  pnl=\${p.get(\"pnl_dollars\",0):+.2f} ({p.get(\"pnl_pct\",0):+.1f}%)  strat={p.get(\"strategy\")}')
" 2>&1

echo
echo "=== /api/status — positions count ==="
curl -s -m 10 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  status.positions     = {d.get(\"positions\")}')
print(f'  status.balance       = \${d.get(\"balance\"):,.2f}')
print(f'  status.broker        = {d.get(\"execution_broker\")}')
print(f'  status.broker_connected = {d.get(\"broker_connected\")}')
pd = d.get('position_details', {}) or {}
print(f'  status.position_details keys: {list(pd.keys())}')
" 2>&1

echo
echo "=== engine state — count equity vs crypto in self.positions ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import requests, os
secret = os.environ.get('DASHBOARD_SECRET_KEY','')
r = requests.get('http://localhost:5000/api/status', auth=('admin', secret), timeout=10).json()
pd = r.get('position_details', {}) or {}
print(f'  count: {len(pd)}')
for sym, pos in pd.items():
    is_crypto = any(sfx in sym.upper() for sfx in ('-USD','-USDT','-BTC','-ETH'))
    print(f'    [{(\\\"CRYPTO\\\" if is_crypto else \\\"EQUITY\\\")}]  {sym:10s}  strat={pos.get(\\\"strategy\\\")}  qty={pos.get(\\\"quantity\\\")}  entry=\${pos.get(\\\"entry_price\\\",0):.4f}')
\" 2>&1"

echo
echo "=== IBKR side — get_positions() directly ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
# Hack: import engine's already-connected broker by checking what's there
import requests, os, json
secret = os.environ.get('DASHBOARD_SECRET_KEY','')
# Try IBKR-direct via a debug endpoint if it exists; otherwise skip
r = requests.get('http://localhost:5000/api/broker/ibkr/positions', auth=('admin', secret), timeout=10)
if r.status_code == 200:
    print(r.json())
else:
    print(f'  no debug endpoint (status {r.status_code})')
\" 2>&1"

echo
echo "=== recent SAFETY GATE BLOCK or BLOCKED SYMBOL (equity entries denied?) ==="
docker logs --since 4h trading-bot-trading-bot-1 2>&1 | grep -E "SAFETY GATE BLOCK|BLOCKED SYMBOL|REJECTED.*momentum|REJECTED.*premarket_gap" | tail -15

echo
echo "=== recent equity buy signals fired? (last 4h) ==="
docker logs --since 4h trading-bot-trading-bot-1 2>&1 | grep -E "Momentum BUY|RVOL SIGNAL|breakout entry" | grep -v "USD\|USDT" | tail -10
