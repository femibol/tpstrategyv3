#!/bin/bash
set +e
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"

echo "=== /api/positions — full body ==="
curl -s -m 15 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" > /tmp/pos.json
python3 -c "
import json
d = json.load(open('/tmp/pos.json'))
print(f'type: {type(d).__name__}, len: {len(d) if hasattr(d,\"__len__\") else \"n/a\"}')
print(json.dumps(d, indent=2)[:4000])
print('---')
print('null-or-missing field audit:')
if isinstance(d, list):
    for i,p in enumerate(d):
        issues=[]
        for k in ['symbol','direction','quantity','entry_price','current_price','stop_loss','trailing_stop','take_profit','pnl_pct','pnl_dollars']:
            if k not in p or p[k] is None: issues.append(k)
        if issues: print(f'  row {i} ({p.get(\"symbol\")}): missing/null = {issues}')
"

echo "=== /api/trades — first 3 entries + null/missing-field audit ==="
curl -s -m 15 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/trades" > /tmp/tr.json
python3 -c "
import json
d = json.load(open('/tmp/tr.json'))
print(f'count: {len(d)}')
print('first 3:')
print(json.dumps(d[:3], indent=2)[:3000])
print('---')
print('null-or-missing field audit across all rows:')
bad = []
for i,t in enumerate(d):
    issues=[]
    for k in ['symbol','direction','entry_price','exit_price','pnl','reason','strategy']:
        if k not in t or t[k] is None: issues.append(k)
    if issues: bad.append((i, t.get('symbol'), issues))
if bad:
    for b in bad[:20]: print(f'  row {b[0]} sym={b[1]}: missing/null = {b[2]}')
    print(f'TOTAL bad rows: {len(bad)}')
else:
    print('  all rows look clean (no null symbol/direction/etc)')
"

echo
echo "=== Monday-through-now trade stats ==="
python3 -c "
import json
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
d = json.load(open('/tmp/tr.json'))
et = ZoneInfo('America/New_York')
# Monday of this week ET = 2026-06-15 00:00 ET
monday = datetime(2026,6,15,0,0,tzinfo=et)
since = []
for t in d:
    ts = t.get('exit_time') or t.get('entry_time')
    if not ts: continue
    try:
        dt = datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone(et)
    except Exception:
        continue
    if dt >= monday: since.append((dt,t))
since.sort(key=lambda x: x[0])
print(f'trades since Mon 06-15 ET: {len(since)}')
if since:
    wins=[t for _,t in since if (t.get(\"pnl\") or 0) > 0]
    losses=[t for _,t in since if (t.get(\"pnl\") or 0) < 0]
    flats=[t for _,t in since if (t.get(\"pnl\") or 0) == 0]
    total_pnl=sum((t.get(\"pnl\") or 0) for _,t in since)
    gross_win=sum((t.get(\"pnl\") or 0) for t in wins)
    gross_loss=abs(sum((t.get(\"pnl\") or 0) for t in losses))
    pf = (gross_win/gross_loss) if gross_loss>0 else float(\"inf\")
    print(f'  wins/losses/flats: {len(wins)}/{len(losses)}/{len(flats)}')
    print(f'  win rate: {len(wins)/len(since)*100:.1f}%')
    print(f'  total P&L: \${total_pnl:+.2f}')
    print(f'  gross win: \${gross_win:.2f} | gross loss: \${gross_loss:.2f} | PF: {pf:.2f}')
    if wins: print(f'  avg win: \${sum((t.get(\"pnl\") or 0) for t in wins)/len(wins):.2f}')
    if losses: print(f'  avg loss: \${sum((t.get(\"pnl\") or 0) for t in losses)/len(losses):.2f}')
    # By day
    by_day={}
    for dt,t in since:
        k=dt.strftime('%a %m-%d')
        by_day.setdefault(k,[]).append(t)
    print('  by day:')
    for k,v in by_day.items():
        pnl=sum((t.get(\"pnl\") or 0) for t in v)
        wr=sum(1 for t in v if (t.get(\"pnl\") or 0)>0)/len(v)*100
        print(f'    {k}: {len(v)} trades, \${pnl:+.2f}, wr {wr:.0f}%')
    # By strategy
    by_strat={}
    for _,t in since:
        k=t.get('strategy','?')
        by_strat.setdefault(k,[]).append(t)
    print('  by strategy:')
    rows=[]
    for k,v in by_strat.items():
        pnl=sum((t.get(\"pnl\") or 0) for t in v)
        wr=sum(1 for t in v if (t.get(\"pnl\") or 0)>0)/len(v)*100
        rows.append((pnl,k,len(v),wr))
    rows.sort(reverse=True)
    for pnl,k,n,wr in rows:
        print(f'    {k}: {n} trades, \${pnl:+.2f}, wr {wr:.0f}%')
"
