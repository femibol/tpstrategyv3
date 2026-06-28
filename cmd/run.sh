#!/bin/bash
set +e
cd /opt/trading-bot
python3 << 'PYEOF'
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
et = ZoneInfo('America/New_York')

with open('data/trade_history.json') as f:
    trades = json.load(f)

def to_et(ts):
    if not ts: return None
    try: return datetime.fromisoformat(ts.replace('Z','+00:00')).astimezone(et)
    except Exception: return None

CUT = datetime(2026,6,18,0,0,tzinfo=et)
post = []
for t in trades:
    dt = to_et(t.get('exit_time') or t.get('entry_time'))
    if dt and dt >= CUT:
        post.append((dt, t))
post.sort(key=lambda x: x[0])

print('=== 15 WORST losers in POST-fix period (≥06-18) ===')
worst = sorted(post, key=lambda x: (x[1].get('pnl') or 0))[:15]
for dt, t in worst:
    print(f'  {dt.strftime("%m-%d %H:%M")}  {t.get("symbol"):10s}  ${t.get("pnl",0):+9.2f}  qty={t.get("quantity")}  entry=${t.get("entry_price",0):.4f} exit=${t.get("exit_price",0):.4f}  {t.get("reason")}  [{t.get("strategy")}]')

print()
print('=== Is the big crypto loss the JUP-USD phantom? ===')
jup = [(dt,t) for dt,t in post if (t.get('symbol') or '').upper()=='JUP-USD']
print(f'JUP-USD trades in POST period: {len(jup)}')
for dt, t in jup:
    print(f'  {dt.strftime("%m-%d %H:%M")}  ${t.get("pnl",0):+9.2f}  qty={t.get("quantity")}  entry=${t.get("entry_price",0):.4f} exit=${t.get("exit_price",0):.6f}  {t.get("reason")}')

print()
print('=== POST losers by exit-price-near-zero (phantom collision detector) ===')
phantom = [(dt,t) for dt,t in post if (t.get("exit_price") or 0) > 0 and (t.get("exit_price") or 0) < 0.01 and abs(t.get("pnl") or 0) > 50]
print(f'trades with exit < $0.01 AND |pnl| > $50 (likely ticker-collision phantoms): {len(phantom)}')
tot_phantom = 0
for dt, t in phantom:
    tot_phantom += (t.get("pnl") or 0)
    print(f'  {dt.strftime("%m-%d %H:%M")}  {t.get("symbol"):10s}  ${t.get("pnl",0):+9.2f}  exit=${t.get("exit_price",0):.6f}  entry=${t.get("entry_price",0):.4f}  {t.get("reason")}')
print(f'  TOTAL phantom P&L: ${tot_phantom:+.2f}')

print()
print('=== POST P&L with phantoms EXCLUDED ===')
real = [(dt,t) for dt,t in post if not ((t.get("exit_price") or 0) > 0 and (t.get("exit_price") or 0) < 0.01 and abs(t.get("pnl") or 0) > 50)]
def is_crypto(t):
    return any(s in (t.get('symbol') or '').upper() for s in ('-USD','-USDT','-BTC','-ETH'))
def stat(items, label):
    n=len(items)
    if not n: print(f'  {label}: 0'); return
    tot=sum((t.get("pnl") or 0) for _,t in items)
    w=sum(1 for _,t in items if (t.get("pnl") or 0)>0)
    print(f'  {label:22s}: {n}t  ${tot:+.2f}  wr {w/n*100:.0f}%')
stat(real, 'POST all (real)')
stat([x for x in real if not is_crypto(x[1])], 'POST equity (real)')
stat([x for x in real if is_crypto(x[1])], 'POST crypto (real)')
PYEOF
