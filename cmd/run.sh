#!/bin/bash
cd /opt/trading-bot
echo "=== resolve conflict: take main's settings.yaml (has PR #184) ==="
git checkout --theirs config/settings.yaml 2>&1
git add config/settings.yaml
git stash drop stash@{0} 2>&1 | tail -2 || true

echo ""
echo "=== validate YAML ==="
python3 -c "import yaml; yaml.safe_load(open('config/settings.yaml'))" && echo "YAML OK"

echo ""
echo "=== git status after resolve ==="
git status -s | head -10

echo ""
echo "=== restart bot (config bind-mounted, plain restart picks up fix) ==="
docker restart trading-bot-trading-bot-1
sleep 15
docker inspect -f 'state: {{.State.Status}}  health: {{.State.Health.Status}}' trading-bot-trading-bot-1

echo ""
echo "=== bot startup logs ==="
docker logs trading-bot-trading-bot-1 --tail 15 2>&1 | tail -15

echo ""
echo "=== positions after re-sync ==="
sleep 8
python3 -c "
import json
with open('data/positions_state.json') as f:
    pos = json.load(f)
print(f'count: {len(pos)}')
for sym, p in pos.items():
    print(f'  {sym}  qty={p.get(\"quantity\",0):.4g}  strategy={p.get(\"strategy\",\"?\")}')"
