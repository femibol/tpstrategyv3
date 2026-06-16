#!/bin/bash
set +e
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
AUTH=$(printf 'admin:%s' "$DASHBOARD_SECRET_KEY" | base64 -w0)

echo "=== /api/trades (default = limit 100) — first row shape ==="
curl -s -m 12 -H "Authorization: Basic $AUTH" http://localhost:5000/api/trades > /tmp/trades.json
python3 << 'PYEOF'
import json
d = json.load(open('/tmp/trades.json'))
print(f"count: {len(d)}")
if d:
    print("first row keys:", sorted(d[0].keys()))
    last = d[-1]
    print("last row:", {k: last.get(k) for k in ['exit_time','entry_time','symbol','strategy','pnl']})
    print("LAST FIVE:")
    for t in d[-5:]:
        print(f"  {t.get('exit_time','')[:19]} {t.get('symbol','')} {t.get('strategy','')} pnl=${t.get('pnl',0):+.2f}")
PYEOF

echo
echo "=== exit_time format check (today's trades) ==="
python3 << 'PYEOF'
import json, datetime
d = json.load(open('/tmp/trades.json'))
today_et = datetime.datetime.utcnow().date().isoformat()
print(f"today UTC: {today_et}")
todays = [t for t in d if (t.get('exit_time') or '').startswith(today_et)]
print(f"todays count: {len(todays)}")
for t in todays:
    print(f"  exit_time={t.get('exit_time')!r}")
PYEOF
