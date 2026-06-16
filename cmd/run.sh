#!/bin/bash
set +e
echo "=== tailscale binary ==="; command -v tailscale 2>&1 || echo "NOT INSTALLED on host"
echo "=== tailscale version ==="; tailscale version 2>&1 | head -3
echo "=== tailscaled service ==="; systemctl is-active tailscaled 2>&1
echo "=== tailscale status (first 8 lines) ==="; tailscale status 2>&1 | head -8
echo "=== this node MagicDNS name ==="; tailscale status --json 2>/dev/null | grep -oE '"DNSName":"[^"]+"' | head -1
echo "=== existing serve config ==="; tailscale serve status 2>&1 | head -10
echo "=== funnel status ==="; tailscale funnel status 2>&1 | head -5
echo "=== is tailscale running in a container instead? ==="; docker ps --format '{{.Names}} {{.Image}}' 2>&1 | grep -i tailscale || echo "no tailscale container"
