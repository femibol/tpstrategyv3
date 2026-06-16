#!/bin/bash
set +e
echo "=== tailscale status ==="
tailscale status 2>&1 | head -10
echo "=== this node MagicDNS name ==="
NAME=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('Self',{}).get('DNSName','').rstrip('.'))" 2>&1)
echo "name=$NAME"
echo "=== HTTPS available in tailnet? (cert probe) ==="
tailscale cert 2>&1 | head -8
echo "=== configure serve: dashboard on https://NAME/ ==="
tailscale serve reset 2>&1 | tail -2
tailscale serve --bg --https=443 http://localhost:5000 2>&1 | tail -8
echo "=== serve status ==="
tailscale serve status 2>&1 | head -15
echo "=== final dashboard URL ==="
if [ -n "$NAME" ]; then echo "https://$NAME/"; else echo "(no MagicDNS name yet — check tailscale up)"; fi
echo "=== smoke test (from VPS to itself via tailnet) ==="
if [ -n "$NAME" ]; then
  curl -sk -m 8 -o /dev/null -w "HTTP %{http_code}\n" "https://$NAME/health" 2>&1
fi
