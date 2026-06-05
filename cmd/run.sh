docker logs trading-bot-trading-bot-1 --since 2h 2>&1 | grep -B1 -A2 'PendingSubmit\|reject\|Error 2' | tail -40
