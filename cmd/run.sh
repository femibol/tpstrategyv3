#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -30
echo "=== verify strategy config is live (read from container) ==="
docker exec trading-bot-trading-bot-1 python3 -c "
import yaml
d = yaml.safe_load(open('config/strategies.yaml'))
print('rvol_scalp.enabled   =', d['rvol_scalp']['enabled'], '(expect False)')
print('rvol_scalp alloc     =', d['allocation']['rvol_scalp'], '(expect 0.0)')
print('rvol_momentum.enabled=', d['rvol_momentum']['enabled'], '(expect True)')
print('rvol_momentum min_rvol  =', d['rvol_momentum']['min_rvol'], '(expect 4.0)')
print('rvol_momentum min_score =', d['rvol_momentum']['min_score'], '(expect 75)')
print('rvol_momentum max/day   =', d['rvol_momentum']['max_trades_per_day'], '(expect 4)')
" 2>&1
echo "=== confirm bot loaded strategies (log tail) ==="
docker logs --tail 40 trading-bot-trading-bot-1 2>&1 | grep -iE "strateg|enabled|rvol_scalp|loaded" | tail -15
