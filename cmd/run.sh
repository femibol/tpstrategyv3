#!/bin/bash
set +e
cd /opt/trading-bot
echo "=== CORRECT the JUP-USD phantom record in trade_history.json ==="
python3 << 'PYEOF'
import json, shutil
from datetime import datetime

PATH = 'data/trade_history.json'
with open(PATH) as f:
    trades = json.load(f)

# Backup first
bak = PATH + '.bak.pre-jup-correction'
shutil.copy(PATH, bak)
print(f'backup written: {bak}')

fixed = 0
for t in trades:
    sym = (t.get('symbol') or '').upper()
    exit_px = t.get('exit_price') or 0
    pnl = t.get('pnl') or 0
    # The phantom: JUP-USD booked at a near-zero collision exit with a huge loss
    if sym == 'JUP-USD' and 0 < exit_px < 0.01 and pnl < -50:
        entry = t.get('entry_price') or 0
        old_pnl = pnl
        old_exit = exit_px
        # Jupiter was actually flat (~entry) at close. Correct exit to entry,
        # pnl to 0 — the position never really lost ~100%; it was a Coinbase
        # ticker collision (real Jupiter ~$0.21). Tag so it's auditable.
        t['exit_price'] = entry
        t['pnl'] = 0.0
        t['pnl_pct'] = 0.0
        t['reason'] = 'collision_corrected'
        t['_correction_note'] = (
            f'2026-06-28: corrected Coinbase ticker-collision phantom. '
            f'was exit=${old_exit:.6f} pnl=${old_pnl:.2f}; real Jupiter '
            f'was ~flat at entry ${entry:.4f}.'
        )
        fixed += 1
        print(f'  corrected: JUP-USD  was pnl=${old_pnl:.2f} exit=${old_exit:.6f} '
              f'-> pnl=$0.00 exit=${entry:.4f}')

if fixed:
    # Atomic write
    tmp = PATH + '.tmp'
    with open(tmp, 'w') as f:
        json.dump(trades, f, indent=2, default=str)
    import os
    os.replace(tmp, PATH)
    print(f'\nwrote {len(trades)} trades, corrected {fixed} phantom(s)')
else:
    print('no phantom found to correct (already fixed?)')

# Verify
with open(PATH) as f:
    trades2 = json.load(f)
remaining = [t for t in trades2 if (t.get('symbol') or '').upper()=='JUP-USD' and (t.get('pnl') or 0) < -50]
print(f'remaining JUP-USD records with pnl < -$50: {len(remaining)} (expect 0)')
PYEOF

echo
echo "=== recompute POST-fix totals after correction ==="
python3 << 'PYEOF'
import json
from datetime import datetime
from zoneinfo import ZoneInfo
et = ZoneInfo('America/New_York')
trades = json.load(open('data/trade_history.json'))
def to_et(ts):
    try: return datetime.fromisoformat((ts or '').replace('Z','+00:00')).astimezone(et)
    except Exception: return None
def is_crypto(t):
    return any(s in (t.get('symbol') or '').upper() for s in ('-USD','-USDT','-BTC','-ETH'))
CUT = datetime(2026,6,18,0,0,tzinfo=et)
post = [t for t in trades if (lambda d: d and d>=CUT)(to_et(t.get('exit_time') or t.get('entry_time')))]
def stat(items, label):
    n=len(items)
    if not n: print(f'  {label}: 0'); return
    tot=sum((t.get('pnl') or 0) for t in items)
    w=sum(1 for t in items if (t.get('pnl') or 0)>0)
    print(f'  {label:22s}: {n}t  ${tot:+.2f}  wr {w/n*100:.0f}%')
stat(post, 'POST all (corrected)')
stat([t for t in post if not is_crypto(t)], 'POST equity')
stat([t for t in post if is_crypto(t)], 'POST crypto')
PYEOF

echo
echo "=== rebuild performance stats so dashboard/drawdown reflect correction ==="
echo "(restart picks up corrected history on next _rebuild_performance_stats_from_history)"
