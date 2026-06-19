#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -30
echo
echo "=== verify blocked_symbols is now visible at runtime ==="
docker exec trading-bot-trading-bot-1 bash -c "cd /app && python3 -c \"
import sys; sys.path.insert(0, '/app')
from bot.config import Config
cfg = Config()
blocked = cfg.risk_config.get('blocked_symbols', [])
blocked_upper = {s.upper() for s in blocked}
print(f'blocked_symbols count: {len(blocked)} (expect 55)')
print(f'SOXL in list: {\\\"SOXL\\\" in blocked_upper} (expect True)')
print(f'JNUG in list: {\\\"JNUG\\\" in blocked_upper} (expect True)')
print(f'TZA  in list: {\\\"TZA\\\" in blocked_upper}  (expect True)')
print(f'TQQQ in list: {\\\"TQQQ\\\" in blocked_upper} (expect True)')
\" 2>&1"
echo
echo "=== bot log: any BLOCKED SYMBOL warnings since restart? ==="
docker logs --tail 200 trading-bot-trading-bot-1 2>&1 | grep "BLOCKED SYMBOL" | tail -10
