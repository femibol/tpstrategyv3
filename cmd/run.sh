#!/bin/bash
set +e
KEY=$(grep -E '^DASHBOARD_SECRET_KEY=' /opt/trading-bot/.env | head -1 | cut -d= -f2- | tr -d '"'"'"'')

echo "=== /api/status (selected fields) ==="
curl -s -m 10 -u "admin:$KEY" http://localhost:5000/api/status 2>&1 | python3 -c "
import json,sys
d=json.load(sys.stdin)
keys=['running','balance','starting_balance','total_return_pct','daily_pnl','daily_trades','peak_balance','drawdown_pct','positions','strategies_active','total_trades','broker_connected','execution_broker']
for k in keys:
    v=d.get(k)
    if isinstance(v,(dict,list)) and len(str(v))>200: v=f'<{type(v).__name__} len={len(v)}>'
    print(f'{k}: {v}')
print('position_details keys:', list(d.get('position_details',{}).keys()))
" 2>&1

echo "=== /api/positions ==="
curl -s -m 10 -u "admin:$KEY" http://localhost:5000/api/positions 2>&1 | python3 -m json.tool 2>&1 | head -40

echo "=== /api/daily (last 3 days) ==="
curl -s -m 10 -u "admin:$KEY" http://localhost:5000/api/daily 2>&1 | python3 -c "
import json,sys
d=json.load(sys.stdin)
for x in d[-3:]:
    print(x)
" 2>&1
