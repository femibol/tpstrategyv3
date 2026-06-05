docker logs trading-bot-trading-bot-1 --since 5m 2>&1 | grep -iE 'HXHX|ORDER ROUTING|BRACKET' | tail -25
