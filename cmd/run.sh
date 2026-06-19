#!/bin/bash
set +e
cd /opt/trading-bot

echo "=== SOXL: how did it enter the universe today? ==="
# Earliest log mention today
echo "--- first SOXL log line in last 24h ---"
docker logs trading-bot-trading-bot-1 --since 24h 2>&1 | grep "\\bSOXL\\b" | head -3
echo
echo "--- any 'add to watchlist' / 'inject' / 'preset' for SOXL? ---"
docker logs trading-bot-trading-bot-1 --since 24h 2>&1 | grep -iE "SOXL.*(watchlist|inject|preset|added)|Added SOXL|Injected SOXL" | head -10
echo
echo "--- SOXL leveraged-ETF filter activity (did the filter ever DROP SOXL?) ---"
docker logs trading-bot-trading-bot-1 --since 24h 2>&1 | grep -iE "LEVERAGED ETF FILTER.*SOXL" | head -5
echo
echo "--- recent LEVERAGED ETF FILTER lines (last 5) — confirm filter is firing on OTHER ETFs ---"
docker logs trading-bot-trading-bot-1 --since 24h 2>&1 | grep "LEVERAGED ETF FILTER" | tail -5
echo
echo "=== where is SOXL right now in the running engine? ==="
docker exec trading-bot-trading-bot-1 python3 << 'PYEOF'
import json, os
# We can't introspect the live engine easily, but we can check the persisted state
# and config to figure out where SOXL is referenced.

# Check data/watchlist.json if it exists
for p in ['data/watchlist.json', 'data/watchlist_performance.json']:
    if os.path.exists(p):
        try:
            d = json.load(open(p))
            print(f'{p}:')
            print(f'  contents (truncated): {str(d)[:400]}')
        except Exception as e:
            print(f'{p}: ERROR {e}')

# Check config for any hardcoded SOXL
import yaml
for cfg in ['config/settings.yaml', 'config/strategies.yaml', 'config/universe.yaml']:
    if os.path.exists(cfg):
        with open(cfg) as f:
            text = f.read()
        if 'SOXL' in text:
            print(f'\n{cfg} contains SOXL:')
            for i, line in enumerate(text.split('\n'), 1):
                if 'SOXL' in line:
                    print(f'  line {i}: {line.strip()[:120]}')

# Search the persisted runners file
for p in ['data/runner_metadata.json', 'data/signals_generated.json', 'data/auto_runner_history.json']:
    if os.path.exists(p):
        d = open(p).read()
        if 'SOXL' in d:
            print(f'\n{p}: contains SOXL ({d.count("SOXL")} occurrences)')
PYEOF

echo
echo "=== SOXL trade today, full reason context ==="
docker logs trading-bot-trading-bot-1 --since 24h 2>&1 | grep -B2 -A2 "\\bSOXL\\b" | head -50
