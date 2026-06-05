docker logs trading-bot-trading-bot-1 --since 200m 2>&1 | grep -E 'HIBS|BLOCKED SYMBOL' | head -20
