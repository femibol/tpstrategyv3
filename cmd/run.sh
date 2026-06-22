#!/bin/bash
set +e
cd /opt/trading-bot
python3 << 'PYEOF'
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
et = ZoneInfo('America/New_York')
now_et = datetime.now(et)
print(f'Now (ET): {now_et.strftime("%Y-%m-%d %H:%M:%S %A")}')
print()

with open('data/trade_history.json') as f:
    trades = json.load(f)

def to_et(ts):
    if not ts: return None
    try: return datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone(et)
    except Exception: return None

def is_crypto(t):
    sym = (t.get('symbol') or '').upper()
    return any(sfx in sym for sfx in ('-USD','-USDT','-BTC','-ETH'))

# Stat windows
TODAY = datetime(now_et.year, now_et.month, now_et.day, tzinfo=et)
WK_START = TODAY - timedelta(days=now_et.weekday())  # Monday this week
LAST_WK_START = WK_START - timedelta(days=7)
LAST_WK_END = WK_START
DAY7 = TODAY - timedelta(days=7)

def in_range(t, start, end=None):
    dt = to_et(t.get('exit_time') or t.get('entry_time'))
    if dt is None: return False
    if end is None: return dt >= start
    return start <= dt < end

def stats(items, label):
    n = len(items)
    if n == 0:
        print(f'  {label:30s}: NO TRADES'); return
    wins = [t for t in items if (t.get('pnl') or 0) > 0]
    losses = [t for t in items if (t.get('pnl') or 0) < 0]
    total = sum((t.get('pnl') or 0) for t in items)
    gw = sum((t.get('pnl') or 0) for t in wins)
    gl = abs(sum((t.get('pnl') or 0) for t in losses))
    pf = gw/gl if gl > 0 else float('inf')
    print(f'  {label:30s}: {n:3d} trades, ${total:+8.2f}, wr {len(wins)/n*100:>3.0f}%, PF {pf:.2f}')

print('=== HEADLINE ===')
today  = [t for t in trades if in_range(t, TODAY)]
yday   = [t for t in trades if in_range(t, TODAY - timedelta(days=1), TODAY)]
wtd    = [t for t in trades if in_range(t, WK_START)]
last_w = [t for t in trades if in_range(t, LAST_WK_START, LAST_WK_END)]

stats(today,  'today (Sat 06-20)')
stats(yday,   'yesterday (Fri 06-19)')
stats(wtd,    'this week (Mon 06-15→now)')
stats(last_w, 'last week (Mon 06-08→14)')
print()

# Equity vs Crypto split this week
print('=== THIS WEEK — equity vs crypto split ===')
wk_eq = [t for t in wtd if not is_crypto(t)]
wk_cr = [t for t in wtd if is_crypto(t)]
stats(wk_eq, 'equity this week')
stats(wk_cr, 'crypto this week')
print()

# Daily breakdown this week
print('=== DAILY P&L THIS WEEK ===')
days = {}
for t in wtd:
    dt = to_et(t.get('exit_time') or t.get('entry_time'))
    if dt is None: continue
    k = dt.strftime('%a %m-%d')
    days.setdefault(k, []).append(t)
for k in sorted(days, key=lambda x: x.split()[1]):
    v = days[k]
    pnl = sum((t.get('pnl') or 0) for t in v)
    wr = sum(1 for t in v if (t.get('pnl') or 0) > 0)/len(v)*100
    print(f'  {k}  {len(v):3d}t  ${pnl:+8.2f}  wr {wr:>3.0f}%')
print()

# Strategy breakdown this week
print('=== STRATEGY BREAKDOWN THIS WEEK ===')
bs = {}
for t in wtd:
    bs.setdefault(t.get('strategy','?'), []).append(t)
rows = []
for k, v in bs.items():
    n = len(v); pnl = sum((t.get('pnl') or 0) for t in v)
    wr = sum(1 for t in v if (t.get('pnl') or 0) > 0)/n*100 if n else 0
    rows.append((pnl, k, n, wr))
rows.sort(reverse=True)
for pnl, k, n, wr in rows:
    print(f'  {k:24s}  {n:3d}t  ${pnl:+8.2f}  wr {wr:>3.0f}%')
PYEOF

echo
echo "=== balance + open positions right now ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  balance:           \${d.get(\"balance\"):,.2f}')
print(f'  starting balance:  \${d.get(\"starting_balance\"):,.2f}')
print(f'  total return:      {d.get(\"total_return_pct\"):+.2f}%')
print(f'  drawdown:          {d.get(\"drawdown_pct\"):.2f}%')
print(f'  peak balance:      \${d.get(\"peak_balance\"):,.2f}')
print(f'  open positions:    {d.get(\"positions\")}')
print(f'  running:           {d.get(\"running\")}  paused: {d.get(\"paused\")}  broker: {d.get(\"execution_broker\")}')
" 2>&1

echo
echo "=== open positions detail ==="
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  count: {len(d)}')
for p in d:
    print(f'    {p.get(\"symbol\")}  qty={p.get(\"quantity\")}  entry=\${p.get(\"entry_price\",0):.4f}  now=\${p.get(\"current_price\",0):.4f}  pnl=\${p.get(\"pnl_dollars\",0):+.2f} ({p.get(\"pnl_pct\",0):+.1f}%)  strat={p.get(\"strategy\")}')
" 2>&1

echo
echo "=== container health ==="
docker ps --format "{{.Names}}: {{.Status}}" | head -3
