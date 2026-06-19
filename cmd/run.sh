#!/bin/bash
set +e
cd /opt/trading-bot
python3 << 'PYEOF'
import json
from datetime import datetime
from zoneinfo import ZoneInfo
et = ZoneInfo('America/New_York')
now_et = datetime.now(et)
print(f'Now (ET): {now_et.strftime("%Y-%m-%d %H:%M:%S")}')

with open('data/trade_history.json') as f:
    trades = json.load(f)

def to_et(ts):
    if not ts: return None
    try: return datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone(et)
    except Exception: return None

# Today (2026-06-18 ET) and yesterday for comparison
TODAY = datetime(2026,6,18,0,0,tzinfo=et)
YDAY = datetime(2026,6,17,0,0,tzinfo=et)

today = []
yesterday = []
for t in trades:
    dt = to_et(t.get('exit_time') or t.get('entry_time'))
    if dt is None: continue
    if TODAY <= dt: today.append((dt,t))
    elif YDAY <= dt < TODAY: yesterday.append((dt,t))

def stats(items, label):
    n = len(items)
    if n == 0:
        print(f'{label}: NO TRADES YET'); return None
    wins=[t for _,t in items if (t.get('pnl') or 0)>0]
    losses=[t for _,t in items if (t.get('pnl') or 0)<0]
    total=sum((t.get('pnl') or 0) for _,t in items)
    gw=sum((t.get('pnl') or 0) for t in wins)
    gl=abs(sum((t.get('pnl') or 0) for t in losses))
    pf = gw/gl if gl>0 else float('inf')
    print(f'{label}: {n} trades, ${total:+.2f}, wr {len(wins)/n*100:.0f}%, PF {pf:.2f}')
    return items

print()
print('=== TODAY (Thu 2026-06-18 ET) ===')
stats(today, 'today                 ')
stats(yesterday, 'yesterday (06-17) ref')

if today:
    # By strategy
    print()
    print('=== TODAY by strategy ===')
    bs = {}
    for _,t in today:
        bs.setdefault(t.get('strategy','?'), []).append(t)
    rows = []
    for k,v in bs.items():
        n=len(v); wins=sum(1 for t in v if (t.get('pnl') or 0)>0)
        pnl=sum((t.get('pnl') or 0) for t in v)
        rows.append((pnl,k,n,wins/n*100))
    rows.sort(reverse=True)
    for pnl,k,n,wr in rows:
        print(f'  {k:24s} {n:>3}t  ${pnl:+8.2f}  wr {wr:>3.0f}%')

    # By hour
    print()
    print('=== TODAY by hour ET ===')
    bh = {}
    for dt,t in today:
        bh.setdefault(dt.hour, []).append(t)
    for h in sorted(bh):
        v = bh[h]
        n = len(v); pnl = sum((t.get('pnl') or 0) for t in v)
        wr = sum(1 for t in v if (t.get('pnl') or 0)>0)/n*100
        print(f'  {h:02d}:00  {n:>3}t  ${pnl:+8.2f}  wr {wr:>3.0f}%')

    # Recent trades
    print()
    print('=== TODAY most recent 12 trades ===')
    today.sort(key=lambda x: x[0])
    for dt,t in today[-12:]:
        sign = '+' if (t.get('pnl') or 0)>=0 else ''
        print(f'  {dt.strftime("%H:%M")}  {t.get("symbol","?"):6s}  {t.get("strategy","?"):18s}  '
              f'{sign}${t.get("pnl",0):>7.2f}  ({t.get("reason","?")})')

# OPEN POSITIONS RIGHT NOW
print()
print('=== open positions right now (via API) ===')
PYEOF

set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
curl -s -m 10 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  open: {len(d)}')
for p in d:
    print(f'    {p.get(\"symbol\")}  qty={p.get(\"quantity\")}  entry=\${p.get(\"entry_price\",0):.2f}  now=\${p.get(\"current_price\",0):.2f}  pnl=\${p.get(\"pnl_dollars\",0):+.2f} ({p.get(\"pnl_pct\",0):+.1f}%)  strat={p.get(\"strategy\")}')
" 2>&1 || echo "  (api fetch failed)"

echo
echo "=== new-broader-scanner check — any unusual tickers post-deploy? ==="
docker logs trading-bot-trading-bot-1 --since "$(date -u -d '6 hours ago' '+%Y-%m-%dT%H:%M:%S')" 2>&1 \
  | grep -oE "IBKR scanner \[[A-Z_]+\]: [0-9]+ results" | sort | uniq -c | sort -rn | head -10
echo
echo "  TODAY ICCM/SNBR/EHGO/SDOT/YMAT log presence (the names that were 0 yesterday):"
for s in ICCM SNBR EHGO SDOT YMAT QURE FTHM ELTX ALOT; do
  c=$(docker logs trading-bot-trading-bot-1 --since "$(date -u -d '12 hours ago' '+%Y-%m-%dT%H:%M:%S')" 2>&1 | grep -c "\\b$s\\b")
  printf "    %-6s %s\n" "$s" "$c"
done
