#!/bin/bash
set +e
echo "=== tailscale status ==="
tailscale status 2>&1 | head -10
echo "=== tailscale serve status ==="
tailscale serve status 2>&1
echo "=== tailscaled service ==="
systemctl is-active tailscaled 2>&1
echo "=== local dashboard probe ==="
curl -s -m 5 -o /dev/null -w "localhost:5000 -> HTTP %{http_code} in %{time_total}s\n" http://localhost:5000/health
echo "=== tailnet probe (no auth, just hit /health which is public) ==="
curl -s -m 15 -o /dev/null -w "tailnet /health -> HTTP %{http_code} in %{time_total}s\n" https://trading-bot-vps.tail5db65d.ts.net/health
echo "=== if it timed out: restart tailscale serve ==="
NAME="trading-bot-vps.tail5db65d.ts.net"
if ! curl -sk -m 8 https://$NAME/health >/dev/null 2>&1; then
  echo "tailnet broken, resetting serve config..."
  tailscale serve reset 2>&1 | tail -2
  tailscale serve --bg --https=443 http://localhost:5000 2>&1 | tail -5
  echo "=== retest ==="
  for i in 1 2 3; do
    rc=$(curl -s -m 10 -o /dev/null -w "%{http_code}" https://$NAME/health 2>&1)
    echo "attempt $i: HTTP $rc"
    [ "$rc" = "200" ] && break
    sleep 3
  done
fi
echo "=== final tailnet probe with auth ==="
set -a; source /opt/trading-bot/.env 2>/dev/null; set +a
curl -s -m 12 -o /dev/null -w "tailnet /api/status (auth) -> HTTP %{http_code} in %{time_total}s\n" -u "admin:$DASHBOARD_SECRET_KEY" https://trading-bot-vps.tail5db65d.ts.net/api/status
