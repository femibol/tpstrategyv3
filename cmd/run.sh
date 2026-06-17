#!/bin/bash
set +e
cd /opt/trading-bot

NAMES="ICCM SNBR EHGO SDOT YMAT QURE FTHM ELTX ALOT"

echo "=== did scanner / signal pipeline ever see any of these today? ==="
for n in $NAMES; do
  hits=$(grep -c "\\b$n\\b" logs/trading.log 2>/dev/null)
  printf "  %-6s  log hits today: %s\n" "$n" "$hits"
done

echo
echo "=== signal_log.json — did we ever generate ANY signal for these? ==="
python3 << 'PYEOF'
import json
try:
    sigs = json.load(open('data/signal_log.json'))
except Exception as e:
    print(f'cannot read signal_log: {e}'); sigs = []
print(f'total signals in log: {len(sigs)}')
target = set('ICCM SNBR EHGO SDOT YMAT QURE FTHM ELTX ALOT'.split())
found = [s for s in sigs if (s.get('symbol') or '').upper() in target]
print(f'matches across the list: {len(found)}')
for s in found[-10:]:
    print(f'  {s.get("symbol")}  {s.get("action")}  {s.get("strategy")}  {s.get("timestamp","")[:19]}  status={s.get("status","?")}')
PYEOF

echo
echo "=== trade_history.json — did we ever TRADE these? ==="
python3 << 'PYEOF'
import json
trades = json.load(open('data/trade_history.json'))
target = set('ICCM SNBR EHGO SDOT YMAT QURE FTHM ELTX ALOT'.split())
hits = [t for t in trades if (t.get('symbol') or '').upper() in target]
print(f'trades on these names (ever): {len(hits)}')
for t in hits[-10:]:
    print(f'  {t.get("symbol")}  pnl=${t.get("pnl",0):+.2f}  strat={t.get("strategy")}  exit={t.get("exit_time","")[:19]}')
PYEOF

echo
echo "=== what symbols DID our scanner discover today? (last 50 from log) ==="
grep -oE "DYNAMIC[: ][A-Z]{2,5}|SCANNER[: ][A-Z]{2,5}|discovered[: ][A-Z]{2,5}" logs/trading.log 2>/dev/null | tail -50 | sort -u

echo
echo "=== universe size today (TOP_PERC_GAIN scan results) ==="
grep -E "TOP_PERC_GAIN|scan_market|scanner returned" logs/trading.log 2>/dev/null | tail -10
