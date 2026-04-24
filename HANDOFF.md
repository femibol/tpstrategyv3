# Session Handoff

Current state of in-progress work so the next Claude Code session picks up without re-deriving context. Update this file **before** the session maxes out.

---

## Last Updated
2026-04-24 (2) — Gateway auto-recovery + @everyone pager

## Current In-Progress — claude/fix-algo-bot-jhizr (second commit of the day)
After merging PR #117 (liveness check + signal gate + alert escalation), verified
live on the VPS that all four fixes landed cleanly. But the TWS Gateway API was
still wedged (user had to `docker compose restart ib-gateway` manually each time).

Deep dive into alternatives (CPGW+ibeam, TradersPost, Questrade, TradingView)
surfaced real blockers for a clean migration:
- **CPGW Web API has hard limits** incompatible with this bot's usage: ~5
  concurrent streaming symbols/session (bot streams dozens), scanner endpoint
  returns symbol/name/conid only (bot uses rich scanner data), 60 new 1m-bar
  subscriptions per 10 min (bot rotates faster).
- **ibeam** has real production issues: stale bundled CPGW JAR, Chrome page
  crashes requiring full container rm+recreate.
- **IBKR OAuth for retail**: not available yet, no ETA.
- **TradersPost + IBKR** uses the same CPGW under the hood — pushes the pain
  onto their infra but loses control.

Conclusion: stay on TWS Gateway for now, double-down on resilience. This commit:

1. **Auto-recovery via Docker socket** — `engine.py:_try_auto_recover_gateway()`.
   When background reconnect fails 10 times (~5 min, the same trigger as the
   existing Discord alert), bot restarts the `ib-gateway` container via
   `/var/run/docker.sock`. Safety caps: 3/day, 10-min cooldown, auto-halts at
   cap with escalation alert. Requires docker SDK (added to requirements.txt)
   and socket volume (added to docker-compose.yml).
2. **@everyone on critical alerts** — `notifications.py`. `system_alert(level="error")`
   now prepends `@everyone` and sets `allowed_mentions` so Discord webhook
   mentions actually fire. Previously messages sat in a muted channel for
   22h. Now phone buzzes within ~5 min of outage start.
3. Same branch as PR #117 fixes — stacks on top of liveness check.

**Deploy on VPS after merge:**
```bash
git fetch origin && git checkout main && git pull
docker compose build trading-bot
docker compose up -d --force-recreate trading-bot
```
The docker-compose volume change picks up on recreate; no need to restart
ib-gateway itself. First outage after this deploys should produce a phone
push notification within 5 min and auto-restart the gateway within 5 min;
bot should come back online ~2 min after that.

## Known ruled-out migration paths (for future sessions)
- CPGW + ibeam + ibind — 5-stream cap + weak scanner doesn't fit this bot
- Questrade — viable but major rewrite (new broker module + scanner from scratch),
  tabled as warm-standby only
- TradingView Pine-script + TradersPost — would lose Python strategies, AI
  insights, auto-tuner, learning system. Rejected.

## Last Updated (previous entry)
2026-04-24 — IBKR liveness-check fix (why the bot wasn't trading for 22h)

## In Progress — claude/fix-algo-bot-jhizr (NOT YET MERGED)
User reported "only 2 trades in 2-3 weeks" on 2026-04-24. Live diagnosis
via `docker compose logs trading-bot`:
- `signals=6→approved=6 | positions=0/10` — signals generated + approved,
  but every one dies at execution with `IBKR NOT CONNECTED`.
- 1,739× `IBKR NOT CONNECTED` since Apr 23 20:35 restart.
- 378× `BACKGROUND RECONNECT: attempt #N` — every one times out.
- `docker compose ps` shows `ib-gateway ... Up 5 hours (healthy)` — the
  healthcheck (`cat /proc/net/tcp | grep ':0FA2'`) only tests that the
  TCP port is listening. API handshake is dead. Classic stuck-dialog /
  wedged IBC state.
- Bot was generating `SIGNAL: Momentum BUY ... Vol=0.0x` on Yahoo-delayed
  data (explicit log: "IBKR not connected... falling back to Yahoo"),
  e.g. `NFLX @ $92.42` — a stale price. That masked the outage in the
  dashboard which showed "approved" signals.

**Immediate unblock** (done by user on VPS): `docker compose restart ib-gateway`.

**Four fixes on branch `claude/fix-algo-bot-jhizr`:**
1. **Real liveness check** — `bot/brokers/ibkr.py:is_connected()` now runs
   `reqCurrentTimeAsync()` with a 2s timeout, cached 10s. Previously
   trusted `ib_insync.isConnected()` which only checks TCP. Catches
   wedged-API state.
2. **Signal suppression gate** — `bot/engine.py:_run_strategies()` hard
   returns `[]` when broker not live. Stops phantom signals on stale
   fallback data (root cause of misleading "approved=6 / positions=0"
   heartbeat). Warns once every 5 min so log isn't spammed.
3. **Loud escalation** — `bot/engine.py:_start_background_reconnect()`
   posts CRITICAL log + Discord `system_alert` at attempt 10 (~5 min)
   and every 20 attempts thereafter (~10 min cadence). User is paged
   instead of silently burning hours in degraded mode. Also sends a
   success alert on reconnect.
4. **bars_warm=0/0 display fix** — `engine.py:1252-1266` read `.symbols`
   which doesn't exist on MarketDataFeed. Now reads `_bars_cache.keys()`.

**Still pending**: merge + deploy. Verify on VPS after merge:
- `grep "Real liveness check" bot/brokers/ibkr.py` — confirms deploy
- Heartbeat should now show real `bars_warm=N/M` numbers
- If gateway wedges again, Discord gets a `[ERROR] IBKR reconnect
  failing: 10 consecutive attempts...` alert within 5 min

## Recently Shipped (merged to main)
- **PR #106** (approx) — `ceed18f` Enable mean_reversion for sideways regime resilience (`mean_reversion: 15%`, `momentum_runner: 35%`, was 0% / 50%). Regime detector's built-in multipliers (×1.4 in SIDEWAYS, ×0.6 in BULLISH for mean_reversion) now have a base to scale.
- **PR #105** — Scanner price ceiling filter: dynamic IBKR scanner hits above `scanner_max_price` ($500) dropped at injection time. No more phantom META/NVDA buy signals.
- **PR #104** — Cycle heartbeat INFO log, IBKR-primary honest log, bind-mount `data/`+`logs/` to host, phantom `Score 0 < min 40` fix (risk manager stamps `_rejection_reason`; Discord shows real reason; momentum emits `score`+`rvol`).
- **PR #103** — Dashboard: removed redundant bottom bar.
- **PR #102** — Dashboard overhaul.
- **PR #101** — IBKR is source of truth for capital.

## Current Live State (VPS @ 50.116.54.226)
- **Git**: VPS was on branch `claude/research-premarket-gainers-EFK9r`. User switched to `main` for the 2026-04-20 deploy (confirmed Done). Verify with `git branch --show-current` at session start.
- **Docker**: trading-bot + ib-gateway compose services; bind-mounts for `data/` and `logs/` so host tails work.
- **IBKR**: paper account, no 2FA. Gateway went unhealthy over weekend — a stuck post-login dialog ("GATEWAY" popup IBC couldn't auto-click). Fixed with `docker compose restart ib-gateway` + VNC-in (user had to reach VNC at `<vps_ip>:5900`, not `127.0.0.1:5900` — that was a recurring confusion).
- **Strategies loaded** after 2026-04-20 deploy should be **8** (was 7): momentum 15%, momentum_runner 35%, rvol_momentum 10%, rvol_scalp 5%, prebreakout 5%, premarket_gap 5%, daily_trend_rider 15%, **mean_reversion 15%**. User confirmed "Done" but did NOT paste strategy-list log — verify on next session.

## Still Pending / Gotchas
- **VPS default branch confusion.** VPS sometimes sits on a `claude/*` branch rather than `main` — then `git pull` says "Already up to date" even when main has new commits. Always verify with `git branch --show-current` + `git log --oneline -3` before assuming code deployed.
- **Bar warmup after restart.** Momentum needs 40× 5m bars (~3.3h). Every `--force-recreate` wipes the in-memory bar buffer. First trade after restart typically not before noon ET.
- **Every recent session has been SIDEWAYS regime.** Watch for that in the new cycle heartbeat log line. If still sideways, mean_reversion should now be active (`SIGNAL: ... mean_reversion ...` in trading.log).
- **VNC port 5900** is exposed publicly (`0.0.0.0:5900` in docker-compose). Works but risky. Offer to bind-localhost-only in a future session.
- **IB Gateway stuck-dialog recurrence** — if it happens again, `docker compose restart ib-gateway` usually clears it in 2 min; else VNC in.
- **Strategy-level rejections at DEBUG.** `momentum.py:49` and similar log skip reasons at DEBUG. If strategies are silent but cycle heartbeat shows 0 signals, we can't yet see *why* at INFO.

## Next Up (if user wants more)
- Verify after 2026-04-20 deploy: 8 strategies loaded, `CYCLE #N` heartbeat firing, mean_reversion signals appearing.
- Bump strategy-level skip reasons from DEBUG → INFO (or add gauge counts to the heartbeat line).
- Bind VNC (5900) to localhost only for security; SSH-tunnel required for future use.
- PR #41 stale — verify or close.

## Trade Data Locations (from CLAUDE.md)
- `data/trade_history.json` — every closed trade (now bind-mounted to host)
- `data/signal_log.json` — every TradersPost webhook signal (N/A for this user, IBKR-only)
- `logs/trading.log` — main bot log (now bind-mounted)
- `logs/trades.log` — trade-only log

## How to Use This File
- **Start of session**: read this first, then `git log --oneline -10` + `git branch --show-current` (on VPS if deploying).
- **End of session**: update "Last Updated", move merged items to "Recently Shipped", record open work, push to the working branch.
