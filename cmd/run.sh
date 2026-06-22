#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -25
echo
echo "=== verify anti-collision guard is live ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
curl -s -m 10 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/positions" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'positions count: {len(d)}')
for p in d:
    print(f'  {p.get(\"symbol\"):10s}  entry=\${p.get(\"entry_price\",0):.4f}  now=\${p.get(\"current_price\",0):.6f}  pnl=\${p.get(\"pnl_dollars\",0):+.2f} ({p.get(\"pnl_pct\",0):+.2f}%)  source={p.get(\"price_source\")}')
" 2>&1
echo
echo "=== expect JUP-USD source = engine_anti_collision (was coinbase_live) ==="
