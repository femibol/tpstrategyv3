#!/bin/bash
set +e
cd /opt/trading-bot

echo "=== container uptime + health ==="
docker ps --format "table {{.Names}}\t{{.Status}}\t{{.Image}}" | head -5
echo
echo "=== git SHA the container is on ==="
docker exec trading-bot-trading-bot-1 git rev-parse HEAD 2>&1 | head -1
echo
echo "=== boot log: any errors / tracebacks since restart? ==="
docker logs --tail 400 trading-bot-trading-bot-1 2>&1 | grep -iE "ERROR|Traceback|Exception|FATAL" | head -15
echo
echo "=== safety guards firing? (last hour) ==="
docker logs --since 1h trading-bot-trading-bot-1 2>&1 | grep -E "BLOCKED SYMBOL|WATCHLIST BLOCKED|SAFETY GATE BLOCK|LEVERAGED ETF FILTER|FALLING KNIFE PASS" | head -10
echo
echo "=== runtime invariants ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import sys; sys.path.insert(0, '/app')
from bot.config import Config
r = Config().risk_config
checks = {
    'blocked_symbols count':       len(r.get('blocked_symbols', [])),
    'profit_taking enabled':       r.get('profit_taking', {}).get('enabled'),
    'profit_taking tiers':         len(r.get('profit_taking', {}).get('targets', [])),
    'velocity_exits enabled':      r.get('velocity_exits', {}).get('enabled'),
    'breakeven enabled':           r.get('breakeven', {}).get('enabled'),
    'strategy_daily_dd_pause_pct': r.get('strategy_daily_dd_pause_pct'),
    'min_volume':                  r.get('min_volume'),
    'falling_knife_pct':           r.get('falling_knife_pct'),
    'max_total_trades_per_day':    r.get('max_total_trades_per_day'),
    'portfolio max single name':   r.get('portfolio_limits', {}).get('max_single_name_pct'),
    'portfolio max gross expo':    r.get('portfolio_limits', {}).get('max_gross_exposure_pct'),
    'scanner_location':            r.get('scanner_location'),
}
for k, v in checks.items():
    print(f'  {k:32s} = {v!r}')
\" 2>&1"
echo
echo "=== dashboard health (via tailnet) ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
HOST="https://trading-bot-vps.tail5db65d.ts.net"
echo "/health:"
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/health" | head -c 300
echo
echo "/api/status quick fields:"
curl -s -m 8 -u "admin:$DASHBOARD_SECRET_KEY" "$HOST/api/status" | python3 -c "
import sys, json
d = json.load(sys.stdin)
print(f'  running={d.get(\"running\")} paused={d.get(\"paused\")} mode={d.get(\"mode\")}')
print(f'  balance=\${d.get(\"balance\"):.2f}  positions={d.get(\"positions\")}  daily_trades={d.get(\"daily_trades\")}')
print(f'  broker_connected={d.get(\"broker_connected\")} execution_broker={d.get(\"execution_broker\")}')
" 2>&1 || echo "  (api fetch failed)"
