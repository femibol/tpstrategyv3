#!/bin/bash
set +e
cd /opt/trading-bot
echo "=== rebuild log so far ==="
cat /tmp/rebuild.log 2>/dev/null | tail -15
echo
echo "=== containers now ==="
docker ps --format '{{.Names}}: {{.Status}}'
echo
if docker ps --format '{{.Names}} {{.Status}}' | grep -q 'ib-gateway.*Up'; then
    echo "GATEWAY UP — checking bot"
    docker logs --tail 10 trading-bot-trading-bot-1 2>&1 | tail -8
else
    echo "gateway still down — corrupted layer needs force re-extraction. Starting repair:"
    nohup bash -c '
      docker compose stop ib-gateway trading-bot 2>&1
      docker compose rm -f ib-gateway trading-bot 2>&1
      docker rmi -f gnzsnz/ib-gateway:10.37.1r 2>&1
      echo "[repull] fetching fresh gateway image..."
      docker compose pull ib-gateway 2>&1 | tail -2
      docker compose up -d 2>&1
      sleep 40
      docker ps --format "{{.Names}}: {{.Status}}"
      echo FORCE_REPAIR_DONE
    ' > /tmp/repair.log 2>&1 &
    echo "repair running — poll /tmp/repair.log next"
fi
