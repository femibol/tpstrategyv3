#!/bin/bash
set +e
cd /opt/trading-bot
python3 << 'PYEOF'
import json
from datetime import datetime
from zoneinfo import ZoneInfo
et = ZoneInfo('America/New_York')
now_et = datetime.now(et)
print(f'Now (ET): {now_et.strftime("%Y-%m-%d %H:%M:%S %A")}')

with open('data/trade_history.json') as f:
    trades = json.load(f)

def is_crypto(t):
    sym = (t.get('symbol') or '').upper()
    return any(sfx in sym for sfx in ('-USD','-USDT','-BTC','-ETH'))

def to_et(ts):
    if not ts: return None
    try: return datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone(et)
    except Exception: return None

# Today and yesterday (ET)
TODAY = datetime(now_et.year, now_et.month, now_et.day, 0, 0, tzinfo=et)
YDAY  = TODAY.replace(day=TODAY.day - 1) if TODAY.day > 1 else TODAY

today_crypto = []
yday_crypto = []
for t in trades:
    if not is_crypto(t): continue
    dt = to_et(t.get('exit_time') or t.get('entry_time'))
    if dt is None: continue
    if dt >= TODAY: today_crypto.append((dt, t))
    elif dt >= YDAY: yday_crypto.append((dt, t))

def show(items, label):
    n = len(items)
    if n == 0: print(f'{label}: NO TRADES'); return
    pnl = sum((t.get('pnl') or 0) for _,t in items)
    wins = sum(1 for _,t in items if (t.get('pnl') or 0) > 0)
    losses = sum(1 for _,t in items if (t.get('pnl') or 0) < 0)
    print(f'{label}: {n} trades, ${pnl:+.2f}, {wins}W/{losses}L, wr {wins/n*100:.0f}%')
    items.sort(key=lambda x: x[0])
    for dt, t in items:
        sign = '+' if (t.get('pnl') or 0) >= 0 else ''
        partial = '(partial)' if t.get('partial') else ''
        print(f'  {dt.strftime("%H:%M")}  {t.get("symbol"):10s}  strat={t.get("strategy"):16s}  {sign}${t.get("pnl",0):>7.2f}  reason={t.get("reason"):24s} {partial}')

print()
print(f'=== TODAY ({TODAY.strftime("%a %m-%d")} ET) — crypto only ===')
show(today_crypto, 'today crypto    ')
print()
print(f'=== YESTERDAY ({YDAY.strftime("%a %m-%d")} ET) — crypto only ===')
show(yday_crypto, 'yesterday crypto')

# Open crypto positions
print()
print('=== open crypto positions right now ===')
PYEOF

set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
crypto = [p for p in d if '-USD' in (p.get('symbol') or '').upper()]
print(f'  open crypto positions: {len(crypto)}')
for p in crypto:
    print(f'    {p.get(\"symbol\")}  qty={p.get(\"quantity\")}  entry=\${p.get(\"entry_price\",0):.4f}  now=\${p.get(\"current_price\",0):.4f}  pnl=\${p.get(\"pnl_dollars\",0):+.2f} ({p.get(\"pnl_pct\",0):+.1f}%)  strat={p.get(\"strategy\")}')
" 2>&1 || echo "  (api fetch failed)"

echo
echo "=== crypto signals fired today (rejected + approved) ==="
docker logs --since 24h trading-bot-trading-bot-1 2>&1 | grep -E "SIGNAL:.*-USD|APPROVED:.*-USD|REJECTED:.*-USD" | tail -10

echo
echo "=== crypto market sentiment now (top 5 strongest in 3d trend) ==="
docker logs --since 1h trading-bot-trading-bot-1 2>&1 | grep "CRYPTO FAST LANE HEARTBEAT" | tail -1 | grep -oE "[A-Z]+-USD\([^)]*\+[0-9.]+%\)" | head -5
