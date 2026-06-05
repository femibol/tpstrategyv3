docker logs trading-bot-trading-bot-1 --since 8m 2>&1 | grep -E 'BRACKET (FILLED|ORDER placed|PARENT|NOT FILLED)' | tail -30
