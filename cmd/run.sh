docker logs trading-bot-trading-bot-1 --since 6m 2>&1 | grep -E 'CYCLE #|bars_warm|signals=|approved=' | tail -8
