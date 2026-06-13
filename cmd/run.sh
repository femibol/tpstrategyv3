cd /opt/trading-bot && grep -A 15 'max_dollar_risk_per_strategy' config/settings.yaml | head -20 && echo '---STRATEGIES STATE---' && grep -E 'momentum:|rvol_scalp:' config/strategies.yaml | head -5
