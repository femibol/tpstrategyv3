docker ps --format '{{.Names}} {{.Image}}' ; echo '---' ; docker inspect trading-bot-trading-bot-1 --format '{{.HostConfig.Binds}}{{.Mounts}}' 2>&1 | head -3
