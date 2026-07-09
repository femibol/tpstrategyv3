#!/bin/bash
# STAGED WHILE BRIDGE WAS DOWN (2026-07-09). The moment the bridge is
# reinstalled, the cmd-runner executes this: deploy PR #248 + verify.
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -25
echo
echo "=== verify PR #248 per-asset exits + crypto sizing are live ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import sys, yaml; sys.path.insert(0, '/app')
d = yaml.safe_load(open('config/settings.yaml'))
r = d['risk']
pt = r['profit_taking']
eq = pt.get('equity_targets') or []
print(f'equity_targets tiers        = {len(eq)} (expect 7)')
print(f'equity first tier           = {eq[0][\\\"pct_from_entry\\\"] if eq else None} (expect 0.025)')
print(f'crypto ladder first tier    = {pt[\\\"targets\\\"][0][\\\"pct_from_entry\\\"]} (expect 0.005)')
print(f'breakeven equity trigger    = {r[\\\"breakeven\\\"].get(\\\"equity_trigger_pct\\\")} (expect 0.02)')
print(f'mean_reversion risk cap     = {r[\\\"max_dollar_risk_per_strategy\\\"][\\\"mean_reversion\\\"]} (expect 150)')
\" 2>&1"
docker exec trading-bot-trading-bot-1 grep -c "_pt_targets_for\|_be_trigger_for" /app/bot/engine.py
echo "(expect >= 6: 2 defs + 4 call sites)"
echo
echo "=== how much data accumulated while the bridge was down? ==="
python3 -c "
import json
t = json.load(open('data/trade_history.json'))
print(f'total trades now: {len(t)}')
print(f'last trade: {t[-1].get(\"exit_time\")} {t[-1].get(\"symbol\")}')
"
echo
echo "=== containers ==="
docker ps --format '{{.Names}}: {{.Status}}' | head -3
