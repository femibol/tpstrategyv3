ufw allow from 104.28.133.13 to any port 5000 proto tcp && ufw allow from 104.28.133.13 to any port 8080 proto tcp && echo '---NEW STATE---' && ufw status numbered | head -20
