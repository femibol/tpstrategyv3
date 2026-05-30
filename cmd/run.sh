#!/bin/bash
cd /opt/trading-bot

echo "=== container state (now) ==="
docker inspect -f 'state: {{.State.Status}}  health: {{.State.Health.Status}}  uptime: {{.State.StartedAt}}' trading-bot-trading-bot-1

echo ""
echo "=== last 3 healthcheck results ==="
docker inspect --format '{{range .State.Health.Log}}{{.End}} exit={{.ExitCode}} {{.Output}}|{{end}}' trading-bot-trading-bot-1 2>/dev/null | tr '|' '\n' | tail -5

echo ""
echo "=== docker logs tail to see what's happening ==="
docker logs trading-bot-trading-bot-1 --tail 30 2>&1 | tail -30

echo ""
echo "=== confirm DELL really flat per IBKR (not just bot's belief) ==="
SECRET=$(grep -E "^DASHBOARD_SECRET_KEY" .env 2>/dev/null | cut -d= -f2- | tr -d '"' | tr -d "'")
curl -s -m 10 -u admin:"$SECRET" http://localhost:5000/api/positions | head -c 500

echo ""
echo "=== stash list (we have stashed auto-tuner edits) ==="
git stash list

echo ""
echo "=== git status ==="
git status -s | head -10
