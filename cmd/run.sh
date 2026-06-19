#!/bin/bash
set +e
cd /opt/trading-bot

echo "=== runtime config — what blocked_symbols does the bot actually see? ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import sys; sys.path.insert(0, '/app')
from bot.config import Config
cfg = Config()
blocked = cfg.risk_config.get('blocked_symbols', [])
print(f'blocked_symbols type: {type(blocked).__name__}, count: {len(blocked)}')
print(f'SOXL in list: {\\\"SOXL\\\" in {s.upper() for s in blocked}}')
print(f'TQQQ in list: {\\\"TQQQ\\\" in {s.upper() for s in blocked}}')
print(f'JNUG in list: {\\\"JNUG\\\" in {s.upper() for s in blocked}}')
print(f'first 8 entries: {list(blocked)[:8]}')
\" 2>&1"

echo
echo "=== auto-tuner overlay files ==="
docker exec trading-bot-trading-bot-1 bash -c "ls -la /app/data/*.yaml 2>/dev/null"
docker exec trading-bot-trading-bot-1 bash -c "cat /app/data/auto-tuner-overrides.yaml 2>/dev/null | head -30; echo ---; cat /app/data/strategy-tuner-overrides.yaml 2>/dev/null | head -30"

echo
echo "=== JNUG trade context (2026-06-15) — same bypass pattern as SOXL? ==="
python3 << 'PYEOF'
import json
with open('data/trade_history.json') as f:
    trades = json.load(f)
for t in trades:
    if (t.get('symbol') or '').upper() in ('JNUG', 'SOXL', 'TZA'):
        print(f"  {t.get('exit_time','?')[:19]}  {t.get('symbol')}  strat={t.get('strategy')}  pnl=\${t.get('pnl',0):+.2f}  reason={t.get('reason')}")
PYEOF

echo
echo "=== source-level: does _execute_signal even run for these? ==="
echo "(search trade-execution paths that DON'T go through _execute_signal)"
grep -n "Executing.*via IBKR\\|Executing.*via TradersPost\\|place_order\\(" /app/bot/engine.py 2>/dev/null | head -10 || \
docker exec trading-bot-trading-bot-1 grep -n "Executing.*via IBKR\\|Executing.*via TradersPost\\|place_order\\(" /app/bot/engine.py | head -20
