#!/bin/bash
set +e
cd /opt/trading-bot
python3 << 'PYEOF'
import json
from datetime import datetime
from zoneinfo import ZoneInfo
et = ZoneInfo('America/New_York')

# Read the full history file (more than the 100 last that /api/trades returns)
with open('data/trade_history.json') as f:
    trades = json.load(f)
print(f'total history rows: {len(trades)}')

def bucket(t):
    ts = t.get('exit_time') or t.get('entry_time')
    if not ts: return None
    try:
        dt = datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone(et)
        return dt
    except Exception:
        return None

# Last week = Mon 06-08 .. Sun 06-14 ET ; This week = Mon 06-15 .. now
LAST_WK_START = datetime(2026,6,8,0,0,tzinfo=et)
THIS_WK_START = datetime(2026,6,15,0,0,tzinfo=et)
last_wk = []
this_wk = []
for t in trades:
    dt = bucket(t)
    if dt is None: continue
    if LAST_WK_START <= dt < THIS_WK_START: last_wk.append((dt,t))
    elif dt >= THIS_WK_START: this_wk.append((dt,t))

def stats(items, label):
    n = len(items)
    if n == 0:
        print(f'{label}: NO TRADES'); return
    wins=[t for _,t in items if (t.get('pnl') or 0)>0]
    losses=[t for _,t in items if (t.get('pnl') or 0)<0]
    total=sum((t.get('pnl') or 0) for _,t in items)
    gw=sum((t.get('pnl') or 0) for t in wins)
    gl=abs(sum((t.get('pnl') or 0) for t in losses))
    pf = gw/gl if gl>0 else float('inf')
    avg_w = (gw/len(wins)) if wins else 0
    avg_l = (-gl/len(losses)) if losses else 0
    print(f'{label}: {n} trades, ${total:+.2f}, wr {len(wins)/n*100:.0f}%, PF {pf:.2f}, avgW ${avg_w:.2f} / avgL ${avg_l:.2f}')

print()
print('=== HEAD-TO-HEAD ===')
stats(last_wk, 'last week (06-08..06-14)')
stats(this_wk, 'this week (06-15..now) ')

def by_strat(items):
    d={}
    for _,t in items:
        d.setdefault(t.get('strategy','?'), []).append(t)
    out=[]
    for k,v in d.items():
        n=len(v)
        wins=[t for t in v if (t.get('pnl') or 0)>0]
        total=sum((t.get('pnl') or 0) for t in v)
        out.append((total,k,n,len(wins)/n*100 if n else 0))
    out.sort(reverse=True)
    return out

print()
print('=== STRATEGY DELTA (this week vs last week) ===')
lw_map = {k:(p,n,w) for p,k,n,w in by_strat(last_wk)}
tw_map = {k:(p,n,w) for p,k,n,w in by_strat(this_wk)}
strats = sorted(set(list(lw_map.keys())+list(tw_map.keys())))
print(f"  {'strategy':24s}  {'last wk':>20s}    {'this wk':>20s}    delta")
for s in strats:
    l = lw_map.get(s, (0,0,0))
    t = tw_map.get(s, (0,0,0))
    delta = t[0] - l[0]
    arrow = '↓' if delta < -5 else ('↑' if delta > 5 else '·')
    print(f"  {s:24s}  ${l[0]:+8.2f} / {l[1]:>2}t / wr {l[2]:>2.0f}%    "
          f"${t[0]:+8.2f} / {t[1]:>2}t / wr {t[2]:>2.0f}%    ${delta:+8.2f} {arrow}")

print()
print('=== HOUR-OF-DAY HEAT (this week) — verify dead-hour block ===')
by_hr = {}
for dt,t in this_wk:
    h = dt.hour
    by_hr.setdefault(h, []).append(t)
print(f"  {'h ET':>4s}  {'trades':>6s}  {'P&L':>10s}  {'wr':>5s}")
for h in sorted(by_hr):
    v = by_hr[h]
    n = len(v)
    pnl = sum((t.get('pnl') or 0) for t in v)
    wr = sum(1 for t in v if (t.get('pnl') or 0)>0)/n*100
    flag = ''
    if h in (5,14): flag = '  <- DEAD HOUR (should be blocked for equity)'
    print(f"  {h:>4d}  {n:>6d}  ${pnl:+8.2f}  {wr:>4.0f}%{flag}")

print()
print('=== REJECTION REASONS (this week) ===')
rej = {}
for _,t in this_wk:
    r = t.get('reason','?')
    rej.setdefault(r, []).append(t)
rows = []
for r,v in rej.items():
    pnl = sum((t.get('pnl') or 0) for t in v)
    rows.append((len(v), pnl, r))
rows.sort(reverse=True)
print(f"  {'count':>5s}  {'P&L':>10s}  reason")
for n,pnl,r in rows:
    print(f"  {n:>5d}  ${pnl:+8.2f}  {r}")
PYEOF

echo
echo "=== bot log: dead-hour block firing? ==="
grep -ciE "dead.hour|equity_dead_hours|14:00 ET|05:00 ET" logs/trading.log 2>/dev/null | head -3
echo "=== bot log: REJECTED rows this week ==="
grep -cE "REJECTED|risk_blocked|slippage_reject" logs/trading.log 2>/dev/null
