#!/bin/bash
cd /opt/trading-bot

echo "=== SNBR signal lifecycle today — find where it died ==="
docker logs trading-bot-trading-bot-1 --since 12h 2>&1 | grep -E "SNBR" | tail -30

echo ""
echo "=== Where do equity APPROVED signals die today? Sample some ==="
docker logs trading-bot-trading-bot-1 --since 8h 2>&1 | grep -E "APPROVED: buy" | grep -vE "USD" | head -5
echo ""
docker logs trading-bot-trading-bot-1 --since 8h 2>&1 | grep -E "REJECTED: buy|SAFETY GATE|GATE_HIT|cooldown" | grep -vE "USD" | head -15

echo ""
echo "=== Today equity rejection counts by reason ==="
docker logs trading-bot-trading-bot-1 --since 12h 2>&1 | grep -iE "REJECTED: buy|SAFETY GATE BLOCK" | grep -vE "USD" | grep -oE "(Setup broke|momentum hit daily DD|spy_circuit_breaker|daily_trade_cap|strategy_drawdown|daily_drawdown|correlation_cluster|cool.down|Already in position|safety_gate_error)" | sort | uniq -c | sort -rn | head -10

echo ""
echo "=== RNDR -\$64.77 trade today — what happened ==="
docker logs trading-bot-trading-bot-1 --since 10h 2>&1 | grep -iE "RNDR" | grep -iE "buy|sell|enter|exit|stop|TradersPost|approved|SIGNAL" | tail -20

echo ""
echo "=== Auto-tuner ran today — what did it change? ==="
docker logs trading-bot-trading-bot-1 --since 12h 2>&1 | grep -E "AUTO.TUNE|Allocations normalized" | tail -10
