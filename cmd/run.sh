docker logs trading-bot-trading-bot-1 --since 4m 2>&1 | grep -iE 'signal:|approved|skip|reject' | head -25
