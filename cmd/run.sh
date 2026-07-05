#!/bin/bash
set +e
cd /opt/trading-bot
python3 << 'PYEOF'
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
et = ZoneInfo('America/New_York')
now_et = datetime.now(et)
print(f'Review: {now_et.strftime("%Y-%m-%d %H:%M ET %A")}')

trades = json.load(open('data/trade_history.json'))
print(f'total trades in history: {len(trades)}')

def to_et(ts):
    try: return datetime.fromisoformat((ts or '').replace('Z','+00:00')).astimezone(et)
    except Exception: return None
def is_crypto(t):
    return any(s in (t.get('symbol') or '').upper() for s in ('-USD','-USDT','-BTC','-ETH'))

rows = []
for t in trades:
    dt = to_et(t.get('exit_time') or t.get('entry_time'))
    if dt: rows.append((dt,t))
rows.sort(key=lambda x: x[0])
if rows:
    print(f'range: {rows[0][0].date()} -> {rows[-1][0].date()}')

def block(items, label):
    n=len(items)
    if not n: print(f'  {label:26s}: 0'); return
    w=[t for _,t in items if (t.get("pnl") or 0)>0]
    l=[t for _,t in items if (t.get("pnl") or 0)<0]
    tot=sum((t.get("pnl") or 0) for _,t in items)
    gw=sum((t.get("pnl") or 0) for t in w); gl=abs(sum((t.get("pnl") or 0) for t in l))
    pf=gw/gl if gl>0 else 99.9
    aw=(gw/len(w)) if w else 0; al=(-gl/len(l)) if l else 0
    print(f'  {label:26s}: {n:4d}t  ${tot:+9.2f}  wr {len(w)/n*100:>3.0f}%  PF {pf:4.2f}  aW ${aw:+6.2f} aL ${al:+6.2f}')

def wk(dt): return (dt - timedelta(days=dt.weekday())).date()
weeks={}
for dt,t in rows: weeks.setdefault(wk(dt),[]).append((dt,t))

print()
print('=== WEEKLY (all assets) ===')
for w in sorted(weeks): block(weeks[w], f'wk {w}')
print()
print('=== WEEKLY equity ===')
for w in sorted(weeks): block([x for x in weeks[w] if not is_crypto(x[1])], f'wk {w} eq')
print()
print('=== WEEKLY crypto ===')
for w in sorted(weeks): block([x for x in weeks[w] if is_crypto(x[1])], f'wk {w} cr')

# Last 7 days rolling
print()
print('=== LAST 7 DAYS ===')
cut7 = now_et - timedelta(days=7)
last7 = [(dt,t) for dt,t in rows if dt>=cut7]
block(last7, 'last 7d all')
block([x for x in last7 if not is_crypto(x[1])], 'last 7d equity')
block([x for x in last7 if is_crypto(x[1])], 'last 7d crypto')

# Strategy breakdown last 7d
print()
print('=== LAST 7 DAYS by strategy ===')
bs={}
for dt,t in last7: bs.setdefault(t.get('strategy','?'),[]).append((dt,t))
for k in sorted(bs, key=lambda k: -sum((t.get('pnl') or 0) for _,t in bs[k])):
    block(bs[k], k)
PYEOF

echo
echo "=== account state ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/status" > /tmp/st.json
python3 -c "
import json
d=json.load(open('/tmp/st.json'))
print(f'  balance=\${d.get(\"balance\"):,.2f}  start=\${d.get(\"starting_balance\"):,.2f}  return={d.get(\"total_return_pct\"):+.2f}%  peak=\${d.get(\"peak_balance\"):,.2f}  dd={d.get(\"drawdown_pct\"):.2f}%  positions={d.get(\"positions\")}')
" 2>&1 || cat /tmp/st.json
echo
echo "=== container health ==="
docker ps --format '{{.Names}}: {{.Status}}' | head -3
