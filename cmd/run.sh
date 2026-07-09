#!/bin/bash
set +e
cd /opt/trading-bot
echo "=== confirm image damage ==="
docker images
docker system df
echo
echo "=== start background re-pull + rebuild (exceeds 90s runner timeout) ==="
nohup bash -c '
  echo "[$(date -u +%H:%M:%S)] pulling ib-gateway image..."
  docker compose pull ib-gateway 2>&1 | tail -3
  echo "[$(date -u +%H:%M:%S)] rebuilding trading-bot image..."
  docker compose build trading-bot 2>&1 | tail -5
  echo "[$(date -u +%H:%M:%S)] starting stack..."
  docker compose up -d 2>&1
  sleep 30
  docker ps --format "{{.Names}}: {{.Status}}"
  echo "REBUILD_DONE"
' > /tmp/rebuild.log 2>&1 &
echo "rebuild running in background — poll /tmp/rebuild.log with the next command"
