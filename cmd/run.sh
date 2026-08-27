#!/bin/bash
echo "=== 1. date/disk ==="
date -u
df -h / | tail -1
echo "=== 2. containers ==="
docker ps --format '{{.Names}} {{.Status}}'
echo "=== 3. snapshot cron log tail ==="
tail -20 /var/log/claude-snapshot.log 2>/dev/null || ls -la /var/log/ | grep -i claude
echo "=== 4. crontab ==="
crontab -l 2>/dev/null | grep -i claude
echo "=== 5. bot log tail ==="
docker logs --tail 15 tpstrategyv3-bot-1 2>&1 | tail -15
echo "=== 6. last trades in history ==="
python3 -c "
import json
t=json.load(open('/root/tpstrategyv3/data/trade_history.json'))
print('total:',len(t))
for x in t[-5:]:
    print(x.get('exit_time','')[:19], x.get('symbol'), x.get('pnl'))
" 2>/dev/null || tail -c 800 /root/tpstrategyv3/data/trade_history.json
