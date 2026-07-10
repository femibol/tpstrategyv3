#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -6
echo
echo "=== verify PR #252 hardening live in container ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import yaml
r = yaml.safe_load(open('config/settings.yaml'))['risk']
c = yaml.safe_load(open('config/settings.yaml'))['crypto']['risk']
st = yaml.safe_load(open('config/strategies.yaml'))
print(f'crypto.max_daily_loss_dollars = {c.get(\\\"max_daily_loss_dollars\\\")} (expect 300)')
print(f'premarket_gap.enabled         = {st[\\\"premarket_gap\\\"][\\\"enabled\\\"]} (expect False)')
print(f'tradingview_signals.enabled   = {st[\\\"tradingview_signals\\\"][\\\"enabled\\\"]} (expect False)')
\" 2>&1"
echo "--- code presence ---"
docker exec trading-bot-trading-bot-1 grep -c "_gate_crypto_sleeve_daily_loss\|Rule 8.6\|LIVE PREFLIGHT\|score / 12 \* 100\|max(1.5, self.vol_surge)" /app/bot/engine.py /app/bot/main.py /app/bot/risk/manager.py /app/bot/strategies/momentum.py /app/bot/strategies/momentum_runner.py 2>&1
echo
echo "=== momentum_runner: is it NOW passing the score gate? (was 30% dead) ==="
docker logs --since 30m trading-bot-trading-bot-1 2>&1 | grep -E "momentum_runner|RUNNER SIGNAL|score [0-9]+/100" | tail -8
echo "--- any 'score X below min' rejections still? ---"
docker logs --since 30m trading-bot-trading-bot-1 2>&1 | grep -iE "below min|score.*reject" | tail -5
echo
echo "=== containers + disk ==="
docker ps --format '{{.Names}}: {{.Status}}'
df -h / | tail -1
