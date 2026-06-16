#!/bin/bash
# Dashboard HTTPS tunnel — Cloudflare quick tunnel.
#
# The bot dashboard listens on the VPS at http://localhost:5000 (plain HTTP,
# LAN-only). This exposes it over HTTPS via a Cloudflare "quick tunnel" so it
# is reachable from a phone/browser anywhere, with no Cloudflare account and
# no DNS setup. The dashboard's own HTTP Basic auth (DASHBOARD_SECRET_KEY,
# enforced on every route via a before_request hook in bot/dashboard/app.py)
# is what protects it — the tunnel only provides transport + a public URL.
#
# CAVEAT: a quick tunnel's URL is RANDOM and CHANGES whenever the cloudflared
# container restarts. For a stable, bookmarkable URL you need a *named* tunnel
# (Cloudflare account + token) or Tailscale Funnel — see HANDOFF for the
# follow-up. This script is the zero-setup "monitor now" path.
#
# Usage (on the VPS):
#   ./scripts/start-dashboard-tunnel.sh          # start/restart + print URL
#   docker logs cloudflared | grep trycloudflare # re-print the current URL
#   docker rm -f cloudflared                     # tear down
#
# The container runs with --restart unless-stopped, so it survives daemon
# restarts and reboots (the URL changes on each cloudflared restart, though).
set +e

DASH_URL="${DASH_URL:-http://localhost:5000}"

echo "Dashboard health check ($DASH_URL):"
curl -s -m 5 "$DASH_URL/health" | head -c 300; echo

echo "Pulling cloudflared image..."
docker pull -q cloudflare/cloudflared:latest >/dev/null 2>&1

echo "Restarting cloudflared container..."
docker rm -f cloudflared >/dev/null 2>&1
docker run -d --name cloudflared --restart unless-stopped --network host \
  cloudflare/cloudflared:latest tunnel --no-autoupdate --url "$DASH_URL" >/dev/null 2>&1

echo "Waiting for tunnel URL..."
url=""
for i in $(seq 1 20); do
  url=$(docker logs cloudflared 2>&1 | grep -oE 'https://[a-z0-9-]+\.trycloudflare\.com' | head -1)
  [ -n "$url" ] && break
  sleep 3
done

if [ -n "$url" ]; then
  echo ""
  echo "============================================================"
  echo "  Dashboard URL: $url"
  echo "  Login: user 'admin' / password = DASHBOARD_SECRET_KEY"
  echo "============================================================"
else
  echo "URL not found yet — check: docker logs cloudflared | grep trycloudflare"
fi
