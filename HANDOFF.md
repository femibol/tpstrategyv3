# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-15 — **Architecture pivot: execution is now IBKR-direct, TradersPost disabled.** The "TradersPost not working" symptom unravelled into two real bugs (below). Bot is verified up on the VPS: `Connected to IBKR (PAPER) at 127.0.0.1:4002`, `using IBKR as sole broker`, `IBKR streaming active for 95 symbols`, `0` `cannot enter context` errors. A manual test trade ran the full `handle_manual_signal → IBKR` path cleanly (rejected only by legit risk checks).

## ✅ CURRENT STATE: BOT IS UP — IBKR-DIRECT

- **Execution path:** IBKR-direct via the bot's own `ib-gateway` container (`bot/brokers/ibkr.py`, single-threaded `ib_async` worker from PR #148). `TRADERSPOST_WEBHOOK_URL` is **commented out** in the VPS `.env` → `engine.py` leaves `tp_broker = None` → the original IBKR-direct bracket-order path (`engine.py:4303`+) is live. PR #148 already made `ib_async` execution safe, so this carries no contextvars risk.
- **TradersPost is disabled**, not deleted — `bot/brokers/traderspost.py` stays in the tree in case a non-IBKR execution broker is added later.
- Verified on the VPS this session: gateway logs in (`DU7733247`, paper), bot connects first try, streams 95 symbols, `/api/signal` test trade processed cleanly.

## 🔑 The two bugs behind "TradersPost not working"

1. **IBKR one-session-per-username — unsolvable for a shared paper account.** The bot's `ib-gateway` and the TradersPost `ALGO_BOT_IBKR` connection were both logging into the *same* IBKR username. IBKR allows only one active session per username, and one paper account has exactly one username (confirmed via IBKR support — cannot be split, cannot add a second login to the same paper account). So the gateway and TradersPost evicted each other forever ("Session Inactive" on one side, `ConnectionRefused 4002` on the other). Alpaca/Tradier (the easy TradersPost fixes) are US-only — not available in Canada. A second *linked IBKR account* would work but means a fresh IBKR application. Decision: drop TradersPost execution entirely and go IBKR-direct (Option B) — PR #148 already removed the only reason TradersPost was made primary.
2. **Healthcheck IPv4/IPv6 false negative — the real crash-loop cause.** `docker-compose.yml`'s `ib-gateway` healthcheck grep'd only `/proc/net/tcp`, but the gnzsnz gateway binds the API port on the IPv6 wildcard (`:::4002`). So a fully healthy gateway always read as unhealthy → autoheal restarted it → and the bot's own self-heal (Docker socket, fires after 10 failed reconnects) restarted it too → the gateway never got the ~90s it needs to finish booting. Fixed this session: healthcheck now greps `/proc/net/tcp6` as well. Proof it was a false negative: with the bot + autoheal stopped, the gateway came up fine and `/proc/net/tcp6` showed `:0FA2` in state `0A` (LISTEN).

## Still worth doing
- **Restart autoheal** — it was stopped during diagnosis (`docker compose stop trading-bot autoheal`). Once the healthcheck fix is deployed, `docker compose up -d` brings it back; verify the gateway now reads `(healthy)`.
- Re-run a real test trade during market hours on a symbol with no existing position (e.g. `MSFT`) to see an actual fill, not just a clean reject (after-hours auto-cancels at 15s).
- After-hours auto-cancel quirk — engine cancels MARKET orders that don't fill in 15s, which catches every after-hours order IBKR queues for the next open. Then logs a misleading `NO EXECUTION PATH AVAILABLE — Set TRADERSPOST_WEBHOOK_URL`. Working as designed but suboptimal — could special-case PreSubmitted orders that IBKR has accepted-but-queued.
- Deploy this branch to the VPS (the `.env` change is already done there manually; this branch makes the healthcheck + doc changes permanent).

## TradersPost mirror mode (DEPLOYED 2026-05-15, end-to-end fill not yet seen)
- `TRADERSPOST_MIRROR_WEBHOOK_URL` (in `.env`) sends every IBKR fill (entries + closes) to a separate TradersPost webhook for visualization. Pure HTTPS notify — never an execution path.
- Wired in `engine.py` (`self.tp_mirror`) and `bot/brokers/traderspost.py` (constructor takes `webhook_url_override`).
- **TradersPost-side requirement:** the subscription this URL points at MUST use TradersPost's built-in Paper Trading broker, NOT a connection to the same IBKR login as `IB_USERNAME` — that revives the session war.
- **VPS state:** `.env` has `TRADERSPOST_MIRROR_WEBHOOK_URL=...fe7bd4dc03b4bb4616887d666ba21246`; bot boot log confirms `TradersPost MIRROR enabled — IBKR fills will be mirrored to ...bb4616887d666ba21246`. After-hours NVDA test trade went IBKR→PreSubmitted→15s-cancel (the documented after-hours quirk), so the mirror webhook itself has not fired yet — needs a real RTH fill to verify end-to-end.
- **⚠️ Compose-env gotcha (cost a rebuild cycle this session):** `docker-compose.yml`'s `trading-bot` service does NOT use `env_file:`; it has an explicit `environment:` allowlist (see lines 128–156). Every new env var the bot needs must be added there as `VAR: ${VAR}` or it silently never reaches the container. The first `docker compose up -d --build` looked clean but boot logs were missing the mirror line — only `docker compose exec trading-bot env | grep …` made it obvious. Pattern to remember: any new `.env` var → add to compose `environment:` block in the same change.
- **Next steps:** during RTH, fire `POST /api/signal {"symbol":"<unowned-symbol>","action":"buy","quantity":1}` (provide `price` if the symbol isn't in the streaming list), watch for `TP MIRROR:` lines in `logs/trading.log`, confirm the trade appears in the TradersPost UI.

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
