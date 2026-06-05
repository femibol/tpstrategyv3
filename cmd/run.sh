docker logs trading-bot-trading-bot-1 --since 6m 2>&1 | grep -iE 'HXHX|APPROVED|BRACKET|ORDER ROUTING|REJECTED|QUALITY GATE' | grep -v HEARTBEAT | tail -25
