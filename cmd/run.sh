sleep 60 && docker logs trading-bot-trading-bot-1 --since 8m 2>&1 | grep -iE 'ORDER ROUTING|HXHX|BRACKET|Executing|approved' | grep -v HEARTBEAT | tail -30
