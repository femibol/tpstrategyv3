#!/bin/bash
set +e
cd /opt/trading-bot
bash scripts/deploy-vps.sh main 2>&1 | tail -30
echo "=== verify scanner_location is live (read from container) ==="
docker exec trading-bot-trading-bot-1 python3 -c "
import yaml
d = yaml.safe_load(open('config/settings.yaml'))
print('risk.scanner_location =', repr(d['risk'].get('scanner_location')), '(expect STK.US)')
" 2>&1
echo
echo "=== confirm broker init read the value ==="
docker logs --tail 200 trading-bot-trading-bot-1 2>&1 | grep -iE "scanner.location|STK\\.US|IBKR Broker|broker init" | tail -10
