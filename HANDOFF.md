# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-14 (4) — **BOT RECOVERED. The two-month contextvars crash is fixed and verified.** PR #148 (`e58d3c8`) refactored `bot/brokers/ibkr.py` to run `ib_async` on a single dedicated worker thread; `nest_asyncio` deleted. Deployed to the VPS — bot reaches "Trading engine started", IBKR data + news streaming (94 symbols), 8 strategies loaded, `0` contextvars errors, container healthy.

## ✅ CURRENT STATE: BOT IS UP

- `main` HEAD includes PR #148 (`e58d3c8`, ibkr.py dedicated worker thread) + PR #146 (`56da615`, TradersPost-primary execution).
- VPS deployed and verified: `IBKR worker thread started` → `Connected to IBKR (PAPER)` → `News scanner started (IBKR)` → `IBKR streaming active for 94 symbols` → `Total strategies loaded: 8` → `Trading engine started - entering main loop`. `0` `cannot enter context` errors. Container `Up ... (healthy)`.
- **Architecture now live:** execution → TradersPost webhook (pure HTTPS, PR #146); market data + news → IBKR via the single-threaded `ib_async` worker (PR #148); Claude AI insights/auto-tuner unchanged.
- One verification still worth doing: let it run, force `docker compose restart ib-gateway`, confirm it reconnects with `0` contextvars errors (proves the reconnect path under the new architecture). If that passes, every code path is proven.

## How the fix works (PR #148 — for context)

`ib_async` is NOT thread-safe. The crash came from its synchronous wrappers being driven from many threads (engine loop, APScheduler jobs, reconnect thread, scalp callbacks) under `nest_asyncio`-patched loops — the same `contextvars.Context` entered concurrently. Fix: `ib_async` is now touched from exactly ONE thread.
- A dedicated `ibkr-worker` daemon thread owns the `IB()` object + one event loop. While idle it pumps the loop (`ib.sleep(0.05)`) so streaming/news/heartbeat keep flowing.
- `@_on_worker` decorator wraps all 25 public I/O methods (`connect`, `place_order`, `get_historical_bars`, `subscribe_*`, `scan_market`, `cancel_*`, …) — bodies unchanged, just routed to the worker via `_run()`, which submits a callable and blocks on a `Future`. `_run()` runs inline if already on the worker thread (no self-deadlock).
- NOT decorated: `is_connected`/`is_symbol_invalid`/`get_live_*` cache reads, `reconnect` (only calls decorated `connect`/`disconnect`), `_on_*` callbacks (already on the worker thread), private helpers called only from decorated methods.
- `nest_asyncio` deleted entirely — import, `.apply()` calls, and the `nest-asyncio` dependency in `requirements.txt`.
- `engine.py` needed zero changes — public `IBKRBroker` signatures unchanged.

The earlier diagnosis trail (deps pin / #139 revert / execution reroute / gateway restart all ruled out) is settled — do not re-investigate.

## ⚠️ Cleanups still owed (not urgent — bot runs fine without them)

- **VNC re-secure.** `docker-compose.yml` was temp-edited on the VPS to `0.0.0.0:5900` and `.env` has `VNC_PASSWORD=tempfix123`. Restore the `127.0.0.1:5900:5900` binding (PR #140's intent) and rotate `VNC_PASSWORD`.
- **Rotate `DASHBOARD_SECRET_KEY`** in the VPS `.env` — it was set to a weak/placeholder value during testing.
- **`requirements.txt` header comment** still mentions `aeventkit` "sensitive to this exact version" — harmless, tidy if touching the file.

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
| #144 | Revert PR #139 — crash STILL happened, so #139 was not the (sole) cause | `81dfcab` |
| #145 | HANDOFF TradersPost-architecture decision | `667e6e9` |
| #146 | TradersPost-primary execution — routes order flow off `ib_async` | `56da615` |
| #147 | HANDOFF: ibkr.py refactor build plan | `a082882` |
| #148 | **ibkr.py dedicated worker thread for `ib_async` — THE root-cause fix; `nest_asyncio` deleted** | `e58d3c8` |

All 18 are on `main`. #139 is reverted (#144). The bot now runs: #146 (TradersPost execution) + #148 (single-threaded `ib_async`) together are the working architecture — verified live on the VPS.

## Deployment / ops notes from the session
- **Gateway stuck-dialog**: earlier today `ib-gateway` crash-looped on `IBC exit code 1109` — IBC rewriting `jts.ini` and a full disk prevented the write from persisting. Fixed by clearing disk (`docker container/image/builder prune`, `journalctl --vacuum-size=100M`) — `/` had been showing free space but a stale 2-day-old `trading-bot-trading-bot-run-*` orphan container + 23h of crash-loop logs had exhausted it. The `ib-gateway-data` named volume already persists `/home/ibgateway/Jts`, so once disk was free the gateway booted clean.
- **VNC**: PR #140 bound `5900` to `127.0.0.1`. During the session it was temporarily reverted to `0.0.0.0` on the VPS (`docker-compose.yml` local edit) + `VNC_PASSWORD=tempfix123` added to `.env` so the user could VNC in without an SSH tunnel. **Re-secure this**: restore the `127.0.0.1:5900:5900` binding and rotate `VNC_PASSWORD`.
- **`DASHBOARD_SECRET_KEY`**: set on the VPS but to a weak/placeholder value during testing. Rotate to a real `openssl rand -hex 32` value.
- **IBKR API enable**: the gnzsnz gateway needed the API checkbox enabled once via VNC (Configure → Settings → API → Enable ActiveX and Socket Clients). It's persisted in the `ib-gateway-data` volume now.
- **`docker compose up` name conflict**: if `up -d` fails with "container name already in use", `docker rm -f trading-bot-trading-bot-1` then `docker compose up -d trading-bot`.

## Still Open (deferred, not started)
- **Reconnect-path verification** — confirm a forced `docker compose restart ib-gateway` reconnects cleanly with `0` contextvars errors under the new worker-thread architecture. Bot startup + steady-state are verified; this proves the last code path.
- **PR 7** — split `bot/engine.py` (8 632 lines) into a `bot/engine/` mixin package. From the original 7-PR brief. Deferred — pure structural cleanup, no longer blocked by anything.
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
