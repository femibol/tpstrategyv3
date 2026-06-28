#!/bin/bash
set +e
cd /opt/trading-bot
python3 << 'PYEOF'
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
et = ZoneInfo('America/New_York')
now_et = datetime.now(et)
print(f'Review generated: {now_et.strftime("%Y-%m-%d %H:%M ET %A")}')

with open('data/trade_history.json') as f:
    trades = json.load(f)
print(f'total trades in history: {len(trades)}')

def to_et(ts):
    if not ts: return None
    try: return datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone(et)
    except Exception: return None

def is_crypto(t):
    sym = (t.get('symbol') or '').upper()
    return any(sfx in sym for sfx in ('-USD','-USDT','-BTC','-ETH'))

# Attach dt
rows = []
for t in trades:
    dt = to_et(t.get('exit_time') or t.get('entry_time'))
    if dt is None: continue
    rows.append((dt, t))
rows.sort(key=lambda x: x[0])
if rows:
    print(f'date range: {rows[0][0].strftime("%Y-%m-%d")} → {rows[-1][0].strftime("%Y-%m-%d")}')

def block(items, label):
    n = len(items)
    if n == 0:
        print(f'  {label:28s}: 0 trades'); return
    wins = [t for _,t in items if (t.get('pnl') or 0)>0]
    losses = [t for _,t in items if (t.get('pnl') or 0)<0]
    total = sum((t.get('pnl') or 0) for _,t in items)
    gw = sum((t.get('pnl') or 0) for t in wins)
    gl = abs(sum((t.get('pnl') or 0) for t in losses))
    pf = gw/gl if gl>0 else 99.9
    avg = total/n
    print(f'  {label:28s}: {n:4d}t  ${total:+9.2f}  wr {len(wins)/n*100:>3.0f}%  PF {pf:4.2f}  avg ${avg:+6.2f}')

# === WEEKLY (Mon-Sun) ===
print()
print('=== WEEKLY P&L (all assets) ===')
def week_start(dt):
    return (dt - timedelta(days=dt.weekday())).date()
weeks = {}
for dt, t in rows:
    weeks.setdefault(week_start(dt), []).append((dt,t))
for wk in sorted(weeks):
    block(weeks[wk], f'wk {wk}')

# === WEEKLY split equity vs crypto ===
print()
print('=== WEEKLY — EQUITY only ===')
for wk in sorted(weeks):
    eq = [(dt,t) for dt,t in weeks[wk] if not is_crypto(t)]
    block(eq, f'wk {wk} equity')
print()
print('=== WEEKLY — CRYPTO only ===')
for wk in sorted(weeks):
    cr = [(dt,t) for dt,t in weeks[wk] if is_crypto(t)]
    block(cr, f'wk {wk} crypto')

# === PRE vs POST config fixes (fixes shipped 2026-06-17 → 06-22) ===
print()
print('=== PRE vs POST config-fix inflection (cutoff 2026-06-18 00:00 ET) ===')
CUT = datetime(2026,6,18,0,0,tzinfo=et)
pre = [(dt,t) for dt,t in rows if dt < CUT]
post = [(dt,t) for dt,t in rows if dt >= CUT]
block(pre, 'PRE  (≤06-17)')
block(post, 'POST (≥06-18)')
print('  -- equity split --')
block([(dt,t) for dt,t in pre if not is_crypto(t)], 'PRE equity')
block([(dt,t) for dt,t in post if not is_crypto(t)], 'POST equity')
print('  -- crypto split --')
block([(dt,t) for dt,t in pre if is_crypto(t)], 'PRE crypto')
block([(dt,t) for dt,t in post if is_crypto(t)], 'POST crypto')
PYEOF

echo
echo "=== current account state ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  balance=\${d.get(\"balance\"):,.2f}  start=\${d.get(\"starting_balance\"):,.2f}  total_return={d.get(\"total_return_pct\"):+.2f}%')
print(f'  peak=\${d.get(\"peak_balance\"):,.2f}  drawdown={d.get(\"drawdown_pct\"):.2f}%  positions={d.get(\"positions\")}')
" 2>&1
