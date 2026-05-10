# AlgoBot

Long-only momentum trading bot. IBKR primary, Polygon.io / Yahoo as fallbacks. Claude AI pre-trade validation. Docker-deployed. Flask dashboard for live state.

Personal project — paper trading on IBKR. Not licensed for redistribution (see License below).

## Architecture

See [`CLAUDE.md`](CLAUDE.md) for the full engine map and review checklist. Top-level layout:

- `bot/engine.py` — main trading loop, position management, end-of-day routine
- `bot/brokers/` — `ibkr.py` (primary, via `ib_async`), `traderspost.py` (execution fallback)
- `bot/strategies/` — one file per strategy, all subclasses of `bot/strategies/base.py`
- `bot/data/market_data.py` — IBKR → Polygon → Yahoo fallback chain with broker-aware gating
- `bot/learning/` — trade analyzer, AI insights (Claude), auto-tuner, weekly review
- `bot/dashboard/` — Flask app for live state, P&L, position view
- `config/settings.yaml` — risk limits, overnight settings, broker config
- `config/strategies.yaml` — per-strategy allocation and parameters

## Setup

See [`SETUP.md`](SETUP.md) for end-to-end install + first-run instructions. TL;DR:

```bash
pip install -r requirements.txt
cp .env.example .env   # fill in IBKR + Anthropic + (optional) Polygon / Discord
python run.py paper
```

## Operations

Production runs in Docker on a Linode VPS. Common commands (run from the repo root on the host):

```bash
# Start everything (trading bot + IB Gateway)
docker compose up -d

# Tail the bot log
docker compose logs -f trading-bot

# Restart only the gateway (clears stuck-dialog state)
docker compose restart ib-gateway

# Force-rebuild after a code change
docker compose build trading-bot && docker compose up -d --force-recreate trading-bot

# Stop everything
docker compose down
```

The watchdog posts CRITICAL alerts to Discord (`@everyone` mention) when:
reconnect attempts exceed 10 in a row, the gateway auto-recovery hits its
3/day cap, or any unhandled exception escapes the main loop.

If IB Gateway pops a dialog that IBC can't auto-dismiss, VNC into the gateway
container. After PR #131 the VNC port is bound to localhost — tunnel via SSH:

```bash
ssh -L 5900:localhost:5900 user@<vps-host>
# then connect a VNC client to localhost:5900
```

See [`HANDOFF.md`](HANDOFF.md) for the current state of in-progress work and known
gotchas — read before starting any session.

## Strategies

13 strategies live in `bot/strategies/`. Allocation weights are in
`config/strategies.yaml` (some default to 0% — they're built but parked):

- **momentum** — broadest trend-following, catches what others miss
- **momentum_runner** — primary runner-catcher, 3 entry types, 4-phase ATR trailing stop
- **mean_reversion** — buy oversold drops below the mean; regime detector scales it up in SIDEWAYS markets
- **rvol_momentum** — Trade Ideas Money Machine-style RVOL plays
- **rvol_scalp** — 1-minute ultra-fast breakout scalping on high-RVOL movers
- **prebreakout** — accumulation detection, enter before the breakout
- **premarket_gap** — top pre-market gainers with extreme gap behavior
- **daily_trend_rider** — swing trades on multi-day runners
- **vwap** — VWAP-anchored scalping
- **pairs_trading** — market-neutral statistical arbitrage
- **smc_forever** — Smart Money Concepts / ICT model
- **pead** — Post-Earnings Announcement Drift
- **short_squeeze** — squeeze detection
- **options_momentum** — calls on breakouts, puts on breakdowns

Each strategy emits signals into the engine, which then runs the
defense-in-depth gate stack in `_execute_signal` (rotation, long-only, crypto
block, falling knife, news block, duplicate guard, broker sync, cooldown,
stale-signal age, stale-price drift) before any order is sent.

## Trade Data

- `data/trade_history.json` — every closed trade (last 500)
- `data/signal_log.json` — every TradersPost webhook signal (last 2000)
- `logs/trading.log` — main bot log
- `logs/trades.log` — trade-only log

Both `data/` and `logs/` are bind-mounted to the host so they survive
container recreates.

## License

All rights reserved. This is a personal project; the source is not licensed
for redistribution, modification, or commercial use.
