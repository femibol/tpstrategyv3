curl -s ifconfig.me ; echo ' (external IP)' ; echo '---' ; iptables -L INPUT -n --line-numbers 2>/dev/null | head -15 ; echo '---' ; ufw status 2>/dev/null | head -10
