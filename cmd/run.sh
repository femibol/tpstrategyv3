sleep 150 && echo '---BRACKET ACTIVITY (last 5min)---' && docker logs trading-bot-trading-bot-1 --since 5m 2>&1 | grep -E 'BRACKET (FILLED|ORDER placed|PARENT|NOT FILLED)' | tail -30
