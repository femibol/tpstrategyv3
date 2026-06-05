docker logs trading-bot-trading-bot-1 --since 2h 2>&1 | grep -E 'error|rejection|reject|warning' | grep -iE 'ibkr|order' | tail -30
