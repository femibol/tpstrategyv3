#!/bin/bash
echo "=== bot state ==="
docker inspect -f 'started: {{.State.StartedAt}}  health: {{.State.Health.Status}}' trading-bot-trading-bot-1
echo ""
echo "=== last 2 crypto fast-lane heartbeats ==="
docker logs trading-bot-trading-bot-1 2>&1 | grep "CRYPTO FAST LANE HEARTBEAT" | tail -2
echo ""
echo "=== any BUY signals since restart? ==="
docker logs trading-bot-trading-bot-1 --since 5m 2>&1 | grep -E "FAST LANE HEARTBEAT.*BUY\[|CRYPTO FAST LANE: approved" | tail -5
echo ""
echo "=== any crypto orders sent since restart? ==="
docker logs trading-bot-trading-bot-1 --since 5m 2>&1 | grep -E "TradersPost SUBMITTED|ORDER BUY.*-USD" | tail -3
