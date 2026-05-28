#!/bin/bash
echo "=== bot's known positions ==="
cd /opt/trading-bot
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
print(f'count: {len(pos)}')
for sym in pos: print(f'  - {sym}')
"

echo ""
echo "=== run mirror orphan reconcile (alert-only, NOT closing) ==="
curl -s -u admin:${DASHBOARD_SECRET_KEY:-changeme} -X POST http://localhost:5000/api/reconcile/mirror/run | head -c 4000
echo ""

echo ""
echo "=== last few signal_log entries for CRSR/CBRG/NOK/RKLB/SMCI ==="
python3 -c "
import json
with open('data/signal_log.json') as f:
    sigs = json.load(f)
watch = {'CRSR','CBRG','NOK','RKLB','SMCI'}
hits = [s for s in sigs if s.get('payload',{}).get('ticker') in watch]
for s in hits[-15:]:
    p = s.get('payload', {})
    print(f\"{s.get('timestamp','?')[:19]}  {p.get('ticker','?'):<6s}  {p.get('action','?'):<6s} qty={p.get('quantity','?')}  resp={s.get('http_status','?')}\")
"
