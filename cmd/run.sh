#!/bin/bash
set +e
echo "=== installing tailscale ==="
curl -fsSL https://tailscale.com/install.sh | sh 2>&1 | tail -6
echo "=== version ==="; tailscale version 2>&1 | head -2
systemctl enable --now tailscaled 2>&1 | tail -2; sleep 2
echo "=== tailscaled active? ==="; systemctl is-active tailscaled 2>&1
echo "=== initiating auth (background, capturing login URL) ==="
rm -f /tmp/tsup.log
( timeout 40 tailscale up --hostname=trading-bot-vps > /tmp/tsup.log 2>&1 ) &
url=""
for i in $(seq 1 15); do
  url=$(grep -oE 'https://login\.tailscale\.com/[a-z]/[A-Za-z0-9]+' /tmp/tsup.log 2>/dev/null | head -1)
  [ -n "$url" ] && break
  sleep 2
done
echo "=== AUTH URL ==="; echo "${url:-NOT_FOUND_YET}"
echo "=== raw tsup.log tail ==="; tail -4 /tmp/tsup.log 2>&1
echo "=== status ==="; tailscale status 2>&1 | head -4
