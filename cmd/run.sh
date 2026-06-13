docker logs trading-bot-trading-bot-1 --since 24h 2>&1 | grep -E 'STXX|10:20:4[5-9]|10:20:5|10:21:' | head -25
