#!/bin/bash
set +e
echo "=== before ==="
docker ps -a --filter name=cloudflared --format '{{.Names}} | {{.Status}}'
echo "=== stop + remove cloudflared (Tailscale Serve is the stable replacement) ==="
docker rm -f cloudflared 2>&1 | tail -2
echo "=== after ==="
docker ps -a --filter name=cloudflared --format '{{.Names}} | {{.Status}}' 2>&1 || true
echo "=== confirm tailscale serve still primary ==="
tailscale serve status 2>&1 | head -5
echo "=== confirm dashboard still reachable through tailnet ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
curl -s -m 12 -o /dev/null -w "tailnet /api/status -> HTTP %{http_code} in %{time_total}s\n" -u "admin:$DASHBOARD_SECRET_KEY" https://trading-bot-vps.tail5db65d.ts.net/api/status
echo "=== done ==="
