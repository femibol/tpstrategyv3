#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -25
echo
echo "=== verify crypto cost-gate ratio is live ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import sys; sys.path.insert(0, '/app')
from bot.config import Config
from bot.risk.cost_model import CostModel
cfg = Config()
cm = CostModel(cfg)
print(f'global min_edge_cost_ratio        = {cm.min_edge_cost_ratio}  (expect 2.0)')
print(f'crypto_min_edge_cost_ratio        = {cm.crypto_min_edge_cost_ratio}  (expect 1.5)')
print(f'_ratio_for(equity)                 = {cm._ratio_for(\\\"equity\\\")}  (expect 2.0)')
print(f'_ratio_for(crypto)                 = {cm._ratio_for(\\\"crypto\\\")}  (expect 1.5)')
# Spot-check the live gate with the MATIC-shaped case (78 bp edge)
sig = {'price': 0.08, 'take_profit': 0.08 * 1.0078, 'stop_loss': 0}
passed, reason = cm.passes(sig, 'crypto')
print(f'MATIC 78bp simulated → passed={passed} reason={reason!r}')
\" 2>&1"
echo
echo "=== bot log: any Cost gate REJECTED lines since restart? ==="
docker logs --tail 200 trading-bot-trading-bot-1 2>&1 | grep "Cost gate" | tail -8
