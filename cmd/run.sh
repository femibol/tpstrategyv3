#!/bin/bash
set +e
NAME=$(tailscale status --json 2>/dev/null | python3 -c "import json,sys; d=json.load(sys.stdin); print(d.get('Self',{}).get('DNSName','').rstrip('.'))" 2>&1)
echo "name=$NAME"
echo "=== reset old serve config ==="
tailscale serve reset 2>&1 | tail -3
echo "=== configure serve: dashboard on https://NAME/ ==="
tailscale serve --bg --https=443 http://localhost:5000 2>&1 | tail -5
echo "=== serve status ==="
tailscale serve status 2>&1 | head -15
echo "=== smoke test (VPS → tailnet hostname) ==="
curl -s -m 15 -o /dev/null -w "HTTP %{http_code}\n" "https://$NAME/health" 2>&1
echo "=== smoke test no-auth on / (expect 401) ==="
curl -s -m 15 -o /dev/null -w "HTTP %{http_code}\n" "https://$NAME/" 2>&1
echo "=== final URL ==="
echo "https://$NAME/"
