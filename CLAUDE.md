# Trading Bot — Claude Review Guide

## Session Handoff — READ FIRST
- **Always read `HANDOFF.md` at the start of every session** to pick up in-progress work.
- **Always update `HANDOFF.md` before ending a session** (or when the context window is getting full). Move merged PRs to "Recently Shipped", record open work, next steps, and gotchas. Commit + push it to the working branch so the next session (local or web) sees it.

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

- **Execution: IBKR-direct.** The bot connects to IBKR via its own `ib-gateway` container and places orders directly (`bot/brokers/ibkr.py`, single-threaded `ib_async` worker per PR #148). TradersPost is **disabled** (`TRADERSPOST_WEBHOOK_URL` blank in `.env`) — see Common Issues for why the TradersPost-primary architecture was abandoned.
- **Engine**: `bot/engine.py` — Main trading loop, position management, EOD routine
- **IBKR broker**: `bot/brokers/ibkr.py` — sole data + execution broker
- **TradersPost broker**: `bot/brokers/traderspost.py` — webhook integration, currently dormant (kept in case a non-IBKR execution broker is added later)
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
- **`ib-gateway` crash-loop / bot stuck on `ConnectionRefused 4002`** — the gateway binds the API port on the IPv6 wildcard (`:::4002`). The `docker-compose.yml` healthcheck must grep `/proc/net/tcp6` as well as `/proc/net/tcp`, or it's a permanent false negative: a healthy gateway reads as unhealthy, autoheal kills it, and the bot's own self-heal (Docker socket) kills it too — eternal restart loop. Fixed 2026-05-15.
- **IBKR "Session Inactive" / gateway can't log in** — IBKR allows ONE active session per username, and one paper account has exactly one username (it cannot be split into two logins). The bot's gateway and any TradersPost IBKR connection sharing that login evict each other forever. This is why execution is now IBKR-direct and TradersPost is disabled.
- Manual test trade — `POST /api/signal` on the dashboard (HTTP Basic auth, password = `DASHBOARD_SECRET_KEY`), body `{"symbol":"MSFT","action":"buy","quantity":1}`. Picks up the live price automatically; rejects if a position already exists or risk checks fail.
- TradersPost "rejected" — usually means exit signal sent for position TP doesn't know about (dual-mode mismatch)
- "no open position" — trying to close something already closed
- Rate limiting — 3s global cooldown, 3 signals per 60s per symbol (exits bypass this)
- Empty `TRADERSPOST_API_KEY` is OK — code treats it as optional

## PR Workflow
- **Enable auto-merge on every PR Claude creates** (call `mcp__github__enable_pr_auto_merge` right after `create_pull_request`). The PR then merges itself the moment it's mergeable — clean state, no required checks outstanding.
