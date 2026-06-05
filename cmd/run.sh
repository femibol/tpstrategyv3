docker logs trading-bot-trading-bot-1 --since 4m 2>&1 | grep -E 'CYCLE|bars_warm|main loop|Scanner cycle' | tail -8
