#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -30
echo
echo "=== verify all 12 keys are now under risk: at runtime ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import sys; sys.path.insert(0, '/app')
from bot.config import Config
r = Config().risk_config
print(f'profit_taking.enabled         = {r.get(\\\"profit_taking\\\", {}).get(\\\"enabled\\\")} (expect True)')
print(f'profit_taking.targets count   = {len(r.get(\\\"profit_taking\\\", {}).get(\\\"targets\\\", []))} (expect 10)')
print(f'velocity_exits.enabled        = {r.get(\\\"velocity_exits\\\", {}).get(\\\"enabled\\\")}')
print(f'breakeven.enabled             = {r.get(\\\"breakeven\\\", {}).get(\\\"enabled\\\")}')
print(f'strategy_daily_dd_pause_pct   = {r.get(\\\"strategy_daily_dd_pause_pct\\\")} (expect 0.05)')
print(f'min_volume                    = {r.get(\\\"min_volume\\\")} (expect 200000)')
print(f'max_total_trades_per_day      = {r.get(\\\"max_total_trades_per_day\\\")} (expect 25)')
print(f'falling_knife_pct             = {r.get(\\\"falling_knife_pct\\\")} (expect -5.0)')
print(f'portfolio_limits.max_single_name_pct = {r.get(\\\"portfolio_limits\\\", {}).get(\\\"max_single_name_pct\\\")} (expect 0.25)')
print(f'blocked_symbols count         = {len(r.get(\\\"blocked_symbols\\\", []))} (expect 55)')
\" 2>&1"
echo
echo "=== confirm cost_model still has its own keys ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import yaml
d = yaml.safe_load(open('config/settings.yaml'))
cm = d.get('cost_model', {})
print(f'cost_model keys: {sorted(cm.keys())}')
print(f'cost_model.equity_fee_bps = {cm.get(\\\"equity_fee_bps\\\")}')
\" 2>&1"
