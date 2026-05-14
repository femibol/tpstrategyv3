# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-14 (2) — **Architecture decision made: TradersPost-primary execution, IBKR demoted to data-only.** Reverting PR #139 did NOT fix the crash. Stop patching `ib_async`. Bot is DOWN; stabilize via `1331fc9` rollback, then next session builds the new architecture.

## ⛔ CURRENT STATE: BOT IS DOWN

- `trading-bot` was crash-looping with `RuntimeError: cannot enter context: <_contextvars.Context object> is already entered` on every asyncio socket read — event loop wedged, no trading.
- User stopped the container. `ib-gateway` left running.
- **PR #139 was reverted (PR #144, `81dfcab`) — crash STILL happened.** So #139 was not the cause, or not the only one. Patching `ib_async` is over.

## ✅ IMMEDIATE STABILIZATION (do this first, ~5 min)

Roll the VPS to `1331fc9` — the last commit that ran 28h stable, before any of this session's 13 PRs. Best-available known-good state. Gets the bot trading TODAY while the real rearchitecture happens in its own session.

```bash
cd /opt/trading-bot
git stash                              # set aside local docker-compose VNC edit
git checkout 1331fc9
docker compose build trading-bot
docker rm -f trading-bot-trading-bot-1 2>/dev/null
docker compose up -d trading-bot
sleep 90
docker compose logs trading-bot --tail=100 | grep -c 'cannot enter context'   # want 0
docker compose logs trading-bot --tail=40 | grep -iE 'connected to ibkr|engine started'
```

Caveat: the contextvars bug has recurred for ~2 months across many commits — `1331fc9` is "ran 28h this time," not "permanently immune." It's a *bridge*, not the fix. If it also crashes, the host/gateway/IBKR-API changed underneath us and the rearchitecture below becomes the only path.

## 🏗️ THE ARCHITECTURE: TradersPost-primary execution, IBKR data-only

**Decision (2026-05-14, user call):** stop trying to make `ib_async` order execution + reconnect stable. The `nest_asyncio` contextvars bug has been fought across PRs #124–129, #139, #142 — every patch eventually fails. Instead, **remove `ib_async` from the critical execution path entirely.**

**Why this ends the cycle:** the contextvars crash only happens because `ib_async` coroutines get driven from threads under a nested event loop. A TradersPost webhook is a plain HTTP POST — no asyncio, no `nest_asyncio`, no event loop. Move execution there and that crash class is *structurally impossible* on the path that matters: getting orders filled. IBKR data streaming can still wedge, but it's no longer fatal — it's a degraded data feed, and the Polygon fallback already exists.

**Current wiring (what exists today):**
- `bot/brokers/traderspost.py` (454 lines) — `TradersPostBroker` is fully built: `send_signal()`, `place_order()`, webhook send with retry, 3s global rate limit, dual-mode (live+paper), crypto-webhook routing, signal persistence to `data/signal_log.json`. Added by PR #129 as a *fallback*.
- `bot/engine.py`: `self.broker` = IBKR (primary), `self.tp_broker` = TradersPost. `_execute_signal()` (line ~3792) calls `self.broker.place_order()` (IBKR) first, then falls back to `self.tp_broker.place_order()` at line ~4307 (`if not order and self.tp_broker`).
- Other `self.broker.place_order()` call sites to reroute: engine.py lines ~455, ~4648, ~4891, ~5218, ~6923.

**The rearchitecture (next session's job):**
1. **Flip execution priority.** In `_execute_signal()` and every other `place_order` call site, route to `self.tp_broker` as PRIMARY. Drop (or make last-resort-only) the `self.broker.place_order()` IBKR path. All order entry/exit/stop/partial-close goes through the TradersPost webhook.
2. **Demote IBKR to data-only.** `self.broker` (IBKR) keeps: streaming quotes/bars, historical bars, scanner, real-time price. It NEVER places orders. Its connection wedging is now non-fatal.
3. **Make the IBKR data path non-blocking / non-fatal.** When `ib_async` streaming/reconnect misbehaves, the engine should fall through to the existing Polygon path (`bot/data/market_data.py` already does IBKR→Polygon→Yahoo) instead of crash-looping. Consider: does the contextvars crash originate in streaming callbacks too? If so, the data path may also need the dedicated-event-loop treatment — but as a *reliability* improvement, not a *can't-trade* blocker.
4. **Config:** TradersPost is enabled when `TRADERSPOST_WEBHOOK_URL` is set (`config.traderspost_webhook_url`). Confirm it (and `_secondary` / `_crypto` / `_password` as needed) is set in the VPS `.env`. Webhook→broker linkage is configured on the TradersPost side (their dashboard), not in this repo.
5. **Verify before deploy (paper):** send a manual signal through `_execute_signal`, confirm it reaches TradersPost (`data/signal_log.json` gets the entry, webhook returns 200), confirm IBKR data still streams, confirm a forced `ib_async` hiccup no longer takes the bot down.

**Cost note:** TradersPost is ~$78/mo. User has explicitly accepted this — two months of downtime/debugging outweighs it.

**Optional later step:** if IBKR data *also* proves too unstable, move data fully to Polygon (key already configured) and remove `ib_async` + `nest_asyncio` from the codebase entirely. Not required for the core fix — execution-via-webhook is the load-bearing change.

## Root Cause (confirmed — for context, not for re-investigation)

The `contextvars` re-entry crash is **not** dependency drift (PR #142 pinned all 88 packages, crash persisted) and **not** solely PR #139 (PR #144 reverted it, crash persisted). It is the long-standing `nest_asyncio` fragility: the codebase calls `ib_async` coroutines from threads with already-running loops (APScheduler jobs, reconnect threads, scalp-monitor callbacks), and under load the same asyncio context gets entered concurrently. Fought across PRs #124, #125, #126, #128, #129, #139, #142, #144. **Conclusion: do not patch it again — route around it (architecture above).**

## Today's Merges (2026-05-14 session)

| PR | What | SHA |
| --- | --- | --- |
| #131 | DNS pin (`8.8.8.8`/`1.1.1.1`) on `ib-gateway` | `a2201d8` |
| #132 | Yahoo / yfinance fallback gating + 60s rate limit | `09bcf0e` |
| #133 | Real `README.md` | `a92da1c` |
| #134 | Mid-session HANDOFF update | `ac7cfb6` |
| #135 | Dashboard auth hardening + TradingView webhook tighten | `7148b55` |
| #136 | `tests/` scaffold + 59 unit tests + GH Actions | `b42c9d0` |
| #137 | `requirements.txt` direct deps pinned `>=` → `==` | `0e48c6f` |
| #138 | Mid-session HANDOFF refresh | `ac12b51` |
| #139 | **Auto-recovery rework — SUSPECTED CAUSE of the contextvars crash** | `28a37a3` |
| #140 | Bind VNC port 5900 to localhost | `dbe11c2` |
| #141 | HANDOFF end-of-session update | `d5e2050` |
| #142 | Full 88-package dependency tree pin — did NOT fix the crash | `2ea9fe3` |
| #143 | HANDOFF architecture-pivot writeup | `3ef705a` |
| #144 | **Revert PR #139 — crash STILL happened, so #139 was not the (sole) cause** | `81dfcab` |

All 13 are on `main`. PRs #131–#138, #140–#143 are believed safe and stay on `main`. #139 is reverted. #142 is harmless. **`main` HEAD is good for the next session to branch from — but the *running VPS bot* should be on `1331fc9` per the stabilization step above until the TradersPost rearchitecture ships.**

## Deployment / ops notes from the session
- **Gateway stuck-dialog**: earlier today `ib-gateway` crash-looped on `IBC exit code 1109` — IBC rewriting `jts.ini` and a full disk prevented the write from persisting. Fixed by clearing disk (`docker container/image/builder prune`, `journalctl --vacuum-size=100M`) — `/` had been showing free space but a stale 2-day-old `trading-bot-trading-bot-run-*` orphan container + 23h of crash-loop logs had exhausted it. The `ib-gateway-data` named volume already persists `/home/ibgateway/Jts`, so once disk was free the gateway booted clean.
- **VNC**: PR #140 bound `5900` to `127.0.0.1`. During the session it was temporarily reverted to `0.0.0.0` on the VPS (`docker-compose.yml` local edit) + `VNC_PASSWORD=tempfix123` added to `.env` so the user could VNC in without an SSH tunnel. **Re-secure this**: restore the `127.0.0.1:5900:5900` binding and rotate `VNC_PASSWORD`.
- **`DASHBOARD_SECRET_KEY`**: set on the VPS but to a weak/placeholder value during testing. Rotate to a real `openssl rand -hex 32` value.
- **IBKR API enable**: the gnzsnz gateway needed the API checkbox enabled once via VNC (Configure → Settings → API → Enable ActiveX and Socket Clients). It's persisted in the `ib-gateway-data` volume now.
- **`docker compose up` name conflict**: if `up -d` fails with "container name already in use", `docker rm -f trading-bot-trading-bot-1` then `docker compose up -d trading-bot`.

## Still Open (deferred, not started)
- **PR 7** — split `bot/engine.py` (8 632 lines) into a `bot/engine/` mixin package. From the original 7-PR brief. Deferred. NOTE: this will collide heavily with the dedicated-event-loop refactor — do the event-loop refactor FIRST, then PR 7, or combine them.
- IBKR API ports `4001`/`4002` still bound `0.0.0.0` — could lock to `127.0.0.1` (no host-side caller; bot reaches gateway via shared netns).
- Unused `AUTH_KEY` constant in `bot/dashboard/templates/dashboard.html` — dead since PR #135.

## Trade Data Locations (from CLAUDE.md)
- `data/trade_history.json` — every closed trade
- `data/signal_log.json` — every TradersPost webhook signal
- `logs/trading.log` — main bot log
- `logs/trades.log` — trade-only log

## How to Use This File
- **Start of session**: read this first, then `git log --oneline -10` + `git branch --show-current`.
- **End of session**: update "Last Updated", move merged items to "Recently Shipped", record open work, push to the working branch.
