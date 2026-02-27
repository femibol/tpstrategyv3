# Trading Bot — Claude Review Guide

## Trade Data Locations

When reviewing trades, check these files:

### Completed Trades
- **`data/trade_history.json`** — Every closed trade with entry/exit prices, P&L, strategy, hold time, execution broker. Last 500 trades. Written by `bot/learning/trade_analyzer.py`.

### TradersPost Signals
- **`data/signal_log.json`** — Every webhook signal sent to TradersPost. Includes payload, HTTP response, rejection status, action type. Last 2000 signals. Written by `bot/brokers/traderspost.py`.

### Log Files
- **`logs/trading.log`** — Main bot log with entries, exits, stop adjustments, regime changes
- **`logs/trades.log`** — Trade-only log (entries and exits filtered from main log)

### Google Sheets
- Daily summaries and individual trades are also logged to Google Sheets (if configured)

## Key Architecture

- **Engine**: `bot/engine.py` — Main trading loop, position management, EOD routine
- **TradersPost broker**: `bot/brokers/traderspost.py` — Webhook integration
- **Config**: `config/settings.yaml` — All strategy parameters, overnight settings, risk limits
- **Trade analyzer**: `bot/learning/trade_analyzer.py` — Performance analysis, parameter tuning
- **AI insights**: `bot/learning/ai_insights.py` — Claude-powered trade analysis

## Review Checklist

When asked to review trades:
1. Read `data/trade_history.json` — check win rate, avg P&L, strategy breakdown
2. Read `data/signal_log.json` — check for rejected signals, failed webhooks
3. Grep logs for `REJECTED`, `RATE LIMIT`, `ERROR` — find execution issues
4. Check `config/settings.yaml` overnight section — verify hold/close settings
5. Look at strategy distribution — which strategies are winning/losing

## Common Issues
- TradersPost "rejected" — usually means exit signal sent for position TP doesn't know about (dual-mode mismatch)
- "no open position" — trying to close something already closed
- Rate limiting — 3s global cooldown, 3 signals per 60s per symbol (exits bypass this)
- Empty `TRADERSPOST_API_KEY` is OK — code treats it as optional
