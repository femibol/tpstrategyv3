#!/bin/bash
cd /opt/trading-bot
echo "=== reconcile with verbose + grep secret from .env ==="
SECRET=$(grep -E "^DASHBOARD_SECRET_KEY" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
echo "secret-set: $([ -n "$SECRET" ] && echo yes || echo no)"
curl -s -o /tmp/recon.out -w "HTTP %{http_code}\n" -u admin:"$SECRET" -X POST http://localhost:5000/api/reconcile/mirror/run
cat /tmp/recon.out | head -c 4000
echo ""

echo ""
echo "=== last 20 signal_log entries (any ticker) ==="
python3 -c "
import json
with open('data/signal_log.json') as f:
    sigs = json.load(f)
print(f'total entries: {len(sigs)}')
for s in sigs[-20:]:
    p = s.get('payload', {})
    print(f\"{s.get('timestamp','?')[:19]}  {p.get('ticker','?'):<10s}  {p.get('action','?'):<6s}  resp={s.get('http_status','?')}  reject={s.get('rejected', '-')}\")
"

echo ""
echo "=== distinct tickers in signal_log (last 200 entries) ==="
python3 -c "
import json
with open('data/signal_log.json') as f:
    sigs = json.load(f)
from collections import Counter
tickers = Counter(s.get('payload',{}).get('ticker','?') for s in sigs[-200:])
for t, n in tickers.most_common():
    print(f'  {t}: {n}')
"

echo ""
echo "=== check the docker logs for CRSR/CBRG/NOK closes ==="
docker logs trading-bot-trading-bot-1 --since 30h 2>&1 | grep -iE "CRSR|CBRG|NOK" | tail -15
