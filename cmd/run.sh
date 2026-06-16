#!/bin/bash
set +e
echo "=== docker ==="; docker --version 2>&1
echo "=== dashboard /health (localhost:5000) ==="; curl -s -m 5 http://localhost:5000/health 2>&1 | head -c 300; echo
echo "=== pull cloudflared image ==="; docker pull -q cloudflare/cloudflared:latest 2>&1 | tail -1
echo "=== remove any old cloudflared ==="; docker rm -f cloudflared 2>/dev/null
echo "=== start quick tunnel ==="
docker run -d --name cloudflared --restart unless-stopped --network host \
  cloudflare/cloudflared:latest tunnel --no-autoupdate --url http://localhost:5000 >/dev/null 2>&1
echo "run-exit: $?"
url=""
for i in $(seq 1 18); do
  url=$(docker logs cloudflared 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
  [ -n "$url" ] && break
  sleep 3
done
echo "=== TUNNEL URL ==="; echo "${url:-NOT_FOUND_YET}"
echo "=== status ==="; docker ps --filter name=cloudflared --format '{{.Names}} | {{.Status}}' 2>&1
if [ -z "$url" ]; then echo "=== last logs ==="; docker logs cloudflared 2>&1 | tail -12; fi
