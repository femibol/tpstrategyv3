#!/bin/bash
set +e
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
AUTH=$(printf 'admin:%s' "$DASHBOARD_SECRET_KEY" | base64 -w0)

echo "=== /api/status.positions (count from header card) ==="
curl -s -m 10 -H "Authorization: Basic $AUTH" http://localhost:5000/api/status | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("positions count:", d.get("positions"))
print("position_details keys:", list((d.get("position_details") or {}).keys()))
'

echo "=== /api/positions (what the Positions tab fetches) ==="
curl -s -m 10 -H "Authorization: Basic $AUTH" http://localhost:5000/api/positions > /tmp/pos.json
python3 << 'PYEOF'
import json
d = json.load(open('/tmp/pos.json'))
print("type:", type(d).__name__)
print("count:", len(d))
for p in d[:5]:
    print(f"  {p.get('symbol')} {p.get('strategy')} qty={p.get('quantity')} entry={p.get('entry_price')} cur={p.get('current_price')} pnl=${p.get('pnl_dollars')}")
PYEOF

echo "=== /api/trades (what the Trades tab fetches) ==="
curl -s -m 10 -H "Authorization: Basic $AUTH" http://localhost:5000/api/trades > /tmp/trd.json
python3 << 'PYEOF'
import json
d = json.load(open('/tmp/trd.json'))
print("count:", len(d))
if d:
    print("first row keys:", sorted(d[0].keys())[:8], "...")
    print("last 2:")
    for t in d[-2:]:
        print(f"  {t.get('exit_time','')[:19]} {t.get('symbol')} {t.get('strategy')} pnl=${t.get('pnl',0):+.2f}")
PYEOF

echo "=== through tailnet — same endpoints, with auth ==="
HOST="https://trading-bot-vps.tail5db65d.ts.net"
for ep in status positions trades performance; do
  printf "  /api/%-12s " "$ep"
  curl -s -m 12 -u "admin:$DASHBOARD_SECRET_KEY" -o /tmp/last.json -w "HTTP %{http_code} in %{time_total}s  " "$HOST/api/$ep"
  # Show the shape of the response (count vs object)
  python3 -c "
import json
d = json.load(open('/tmp/last.json'))
print('len=' + str(len(d)) if hasattr(d, '__len__') else 'type=' + type(d).__name__)
" 2>&1
done
