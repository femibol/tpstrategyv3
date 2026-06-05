docker logs trading-bot-trading-bot-1 --since 4m 2>&1 | grep -iE 'HXHX|SIGNAL|APPROVED|REJECTED|QUALITY GATE SKIP|BRACKET|ORDER ROUTING' | grep -v HEARTBEAT | tail -25
