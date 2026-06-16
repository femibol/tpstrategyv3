#!/bin/bash
set +e
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
AUTH=$(printf 'admin:%s' "$DASHBOARD_SECRET_KEY" | base64 -w0)

echo "=== container status ==="
docker ps --filter name=trading-bot-trading-bot --format '{{.Status}}'

echo "=== boot log lines for trade history + perf stats ==="
docker logs trading-bot-trading-bot-1 2>&1 | grep -E "TRADE HISTORY|PERF STATS|trade_analyzer" | tail -8

echo "=== /api/status (key fields via localhost) ==="
curl -s -m 12 -H "Authorization: Basic $AUTH" http://localhost:5000/api/status > /tmp/status.json
python3 << 'PYEOF'
import json
d = json.load(open('/tmp/status.json'))
keys = ["running","balance","total_return_pct","daily_pnl","daily_trades",
        "positions","total_trades","peak_balance","drawdown_pct"]
for k in keys:
    print(f"  {k}: {d.get(k)}")
PYEOF

echo "=== /api/performance (Win Rate fallback source) ==="
curl -s -m 12 -H "Authorization: Basic $AUTH" http://localhost:5000/api/performance > /tmp/perf.json
python3 << 'PYEOF'
import json
d = json.load(open('/tmp/perf.json'))
print(f"  total_trades: {d.get('total_trades')}")
print(f"  win_rate: {d.get('win_rate')}%")
print(f"  profit_factor: {d.get('profit_factor')}")
print(f"  net_pnl: ${d.get('net_pnl')}")
PYEOF

echo "=== /api/trades?limit=5 ==="
curl -s -m 12 -H "Authorization: Basic $AUTH" "http://localhost:5000/api/trades?limit=5" > /tmp/trades.json
python3 << 'PYEOF'
import json
d = json.load(open('/tmp/trades.json'))
print(f"  count: {len(d)}")
for t in d[-3:]:
    print(f"  - {t.get('exit_time','')[:19]} {t.get('symbol','')} {t.get('strategy','')} pnl=${t.get('pnl',0):+.2f}")
PYEOF
