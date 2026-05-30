#!/bin/bash
echo "=== latest crypto fast-lane heartbeat ==="
docker logs trading-bot-trading-bot-1 --since 30m 2>&1 | grep "CRYPTO FAST LANE HEARTBEAT" | tail -2
echo ""
echo "=== crypto signal count last 1h, per strategy + name ==="
docker logs trading-bot-trading-bot-1 --since 1h 2>&1 | grep -E "SIGNAL.*-USD" | grep -oE "(mean_reversion|momentum|crypto_runner)" | sort | uniq -c
echo ""
echo "=== distinct crypto symbols heard from in last 1h ==="
docker logs trading-bot-trading-bot-1 --since 1h 2>&1 | grep -oE "[A-Z]+-USD" | sort | uniq -c | sort -rn | head -20
echo ""
echo "=== blocked crypto entries last 6h ==="
docker logs trading-bot-trading-bot-1 --since 6h 2>&1 | grep -iE "REJECTED.*-USD|BLOCKED.*-USD|cooldown.*-USD|SAFETY GATE.*-USD" | tail -10
echo ""
echo "=== Looser-than-5% trend names (what's actually clearing the gate now)? ==="
docker logs trading-bot-trading-bot-1 --since 30m 2>&1 | grep -oE "CRYPTO FAST LANE HEARTBEAT.*" | tail -1 | tr '|' '\n' | grep -E "neutral|BUY\[|warming"
