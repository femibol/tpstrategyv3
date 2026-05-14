# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-14 (3) — **Diagnosis CONCLUSIVE. Next session = the `bot/brokers/ibkr.py` dedicated-event-loop refactor.** PR #146 (TradersPost-primary execution) is merged + deployed and is correct — but the bot still crashes *at startup* in the `ib_async` connect/streaming path, before "engine started." Every patch this session is exhausted. The full build plan for the real fix is below — open `bot/brokers/ibkr.py` and execute it.

## ⛔ CURRENT STATE: BOT IS DOWN (container stopped)

- `main` HEAD = `56da615` (PR #146 merge). VPS is on `main`, new code confirmed deployed (`grep -c 'EXECUTION ROUTING: TradersPost-primary'` in the container returned `1`).
- The `trading-bot` container crash-loops at **startup** — `RuntimeError: cannot enter context: <_contextvars.Context object> is already entered` — and never reaches "engine started". The crash is in the `ib_async` connect / streaming / news subscription path that runs during boot.
- User has stopped the container (`docker compose stop trading-bot`). `ib-gateway` left running.
- **Cheap shot ruled out:** `docker compose restart ib-gateway` (clean gateway) + bot restart → still crashed (`20` contextvars errors, no "engine started"). The crash is independent of gateway health.

## ✅ WHAT'S ALREADY DONE & CORRECT (do not redo)

- **PR #146 — TradersPost-primary execution (`56da615`).** Every `ib_async` *execution* call in `engine.py` is gated: when `TRADERSPOST_WEBHOOK_URL` is set, order entry/exit/stop/partial-close all route through the TradersPost webhook (pure HTTPS, no asyncio). IBKR `place_order`/`cancel_order`/`get_positions`-on-exit-path/`get_live_price`-spread-check are all behind `not self.tp_broker` gates or `elif self.broker` legacy branches. This is correct and stays. It just isn't *sufficient* — because the bot also calls `ib_async` heavily at **startup** (connect, position sync, stream subscribe, news subscribe), and that's what still crashes.
- `TRADERSPOST_WEBHOOK_URL` is set in the VPS `.env` (user's webhook configured this session).
- `TradersPostBroker` (`bot/brokers/traderspost.py`, 454 lines) — fully built, unchanged, working.

## 🎯 CONCLUSIVE DIAGNOSIS (settled — do not re-investigate)

The `contextvars` re-entry crash is in **`ib_async` itself, driven under `nest_asyncio`, at the connection/data layer.** Proven by elimination this session:
- NOT dependency drift — PR #142 pinned all 88 packages, crash persisted.
- NOT solely PR #139 — PR #144 reverted it, crash persisted.
- NOT the execution path alone — PR #146 routed execution off `ib_async`, crash persisted **at startup**.
- NOT gateway health — clean `ib-gateway` restart, crash persisted.

Root mechanism: `bot/brokers/ibkr.py` calls `ib_async`'s **synchronous wrappers** (`self.ib.connect()`, `self.ib.qualifyContracts()`, `self.ib.reqHistoricalData()`, `self.ib.reqMktData()`, `self.ib.placeOrder()` + `self.ib.sleep()`, `self.ib.reqTickers()`, etc.) from **multiple threads** (engine main loop, APScheduler jobs, the background reconnect thread, scalp-monitor callbacks). Each sync wrapper does `loop.run_until_complete()` on the *calling thread's* loop; `nest_asyncio.apply()` patches those loops to allow nesting; when `ib_async`'s event callbacks fire across these threads, the same `contextvars.Context` gets entered concurrently → `RuntimeError`. `nest_asyncio` is the hack that makes this *possible*; the multi-thread access is what *triggers* it.

## 🏗️ THE BUILD PLAN: `bot/brokers/ibkr.py` dedicated-event-loop refactor

**Goal:** `ib_async` runs in exactly ONE thread that owns exactly ONE event loop, created with `asyncio.new_event_loop()` + `run_forever()` in a daemon thread. That loop is never nested, never re-entered. `nest_asyncio` is deleted entirely. Every public `IBKRBroker` method submits work to that loop from whatever thread calls it via `asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout=...)`. This is the pattern `ib_async`'s own docs prescribe for multi-threaded apps.

**File:** `bot/brokers/ibkr.py` (1792 lines, one class `IBKRBroker`, ~50 methods). This is the whole job — `engine.py` should need few/no changes because the public method signatures stay identical.

### Step 1 — Kill `nest_asyncio`
- Remove the module-level `import nest_asyncio` / `nest_asyncio.apply()` (lines ~34-35).
- Remove the in-`connect()` `nest_asyncio.apply(loop)` block (lines ~128-131).
- Remove `nest-asyncio` from `requirements.txt`.
- Keep the Python-3.14 `asyncio.wait_for` compat shim (lines ~44-69) for now — it's unrelated; revisit only if it conflicts.

### Step 2 — Dedicated loop + thread (in `__init__` or a new `_start_loop()`)
```python
self._loop = asyncio.new_event_loop()
self._loop_thread = threading.Thread(
    target=self._loop.run_forever, daemon=True, name="ibkr-eventloop")
self._loop_thread.start()
```
Create the `IB()` instance and call **all** `ib_async` I/O on this loop's thread — never elsewhere.

### Step 3 — The submit helper
```python
def _run(self, coro, timeout=30):
    """Submit a coroutine to the dedicated ib_async loop from any thread."""
    fut = asyncio.run_coroutine_threadsafe(coro, self._loop)
    try:
        return fut.result(timeout=timeout)
    except Exception as e:
        log.error(f"IBKR _run error: {e}")
        return None
```

### Step 4 — Convert every I/O method to async + `_run`
`ib_async` exposes an `*Async` variant for every blocking call. Convert each public method to build a coroutine and submit it via `_run`. Map:
| sync (current) | async (use inside coroutine) |
| --- | --- |
| `self.ib.connect(...)` | `await self.ib.connectAsync(...)` |
| `self.ib.qualifyContracts(c)` | `await self.ib.qualifyContractsAsync(c)` |
| `self.ib.reqHistoricalData(...)` | `await self.ib.reqHistoricalDataAsync(...)` |
| `self.ib.reqTickers(c)` | `await self.ib.reqTickersAsync(c)` |
| `self.ib.reqSecDefOptParams(...)` | `await self.ib.reqSecDefOptParamsAsync(...)` |
| `self.ib.placeOrder(...)` + `self.ib.sleep(n)` | `self.ib.placeOrder(...)` then `await asyncio.sleep(n)` — inside a coroutine on the loop |
| `self.ib.reqScannerData(...)` | `await self.ib.reqScannerDataAsync(...)` |

**Methods that DON'T need `_run`** — `ib_async` calls that only read cached state (no I/O, safe from any thread): `self.ib.isConnected()`, `self.ib.positions()`, `self.ib.accountValues()`, `self.ib.trades()`, `self.ib.openTrades()`, `self.ib.portfolio()`. Leave `is_connected()` exactly as-is (bare `self.ib.isConnected()`).

**Methods that DO need `_run`** (they do network I/O): `connect`, `disconnect`, `reconnect`, `place_order`, `_place_bracket_order`, `_get_snap_price`, `get_option_chain`, `cancel_order`, `get_historical_bars`, `subscribe_market_data`, `unsubscribe_market_data`, `subscribe_realtime_bars`, `subscribe_realtime_bars_with_callback`, `subscribe_tick_by_tick`, `unsubscribe_tick_by_tick`, `subscribe_news`, `get_news_providers`, `get_news_article`, `subscribe_account_pnl`, `get_order_book`, `cancel_symbol_orders`, `cancel_all_orders`, `close_all_positions`, `scan_market` (+ all `scan_*` wrappers), `qualifyContracts` calls inside the above.

### Step 5 — Event callbacks
`_on_order_status`, `_on_error`, `_on_disconnect`, `_on_pending_tickers`, `_on_news_tick`, `_on_pnl_update`, `_on_realtime_bar`, `_on_tick_data` — these fire ON the dedicated loop's thread (good — single thread). They already use `self._stream_lock` where they touch shared dicts; keep that. They must NOT call `_run` (would deadlock — already on the loop thread); they can touch `self.ib` directly.

### Step 6 — `reconnect()` / `connect()` retry logic
The client-id-increment retry loop in `connect()` stays, but runs as a coroutine on the dedicated loop. `reconnect()` = `disconnect` then `connect`, all via `_run`. No more ad-hoc reconnect threads driving `ib_async` — the dedicated loop is the only place it runs.

### Step 7 — Verify (paper, before any deploy)
1. `python -c "from bot.brokers.ibkr import IBKRBroker"` — imports clean, no `nest_asyncio`.
2. `python -m bot.main --mode paper --no-dashboard` locally (or on VPS after close) — bot reaches **"engine started"**, connects to IBKR, streams data + news, runs 15 min, **ZERO `contextvars` tracebacks**.
3. Confirm an APScheduler job (scanner cycle) and the engine main loop both hit IBKR concurrently without error — that's the exact multi-thread condition that used to crash.
4. Confirm a forced `docker compose restart ib-gateway` → bot reconnects via the dedicated loop, no thread storm, no crash.
5. Only then deploy to VPS.

**Scope estimate:** real but mechanical — ~50 methods, most are a 3-line wrap. The risk is in `connect()`/streaming/callbacks; do those carefully. It's a focused session's work. Land it as a PR the user reviews; do NOT auto-merge; do NOT deploy unverified.

## Bridge option (if the bot must trade before the refactor lands)
`git checkout 1331fc9` on the VPS + rebuild — last commit that ran 28h (pre-session). It is NOT immune (same `ib_async`/`nest_asyncio`), it just happened to boot cleanly that day. A coin-flip bridge, not a fix. The refactor is the fix.

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
| #146 | **TradersPost-primary execution — routes order flow off `ib_async` (correct, deployed, but bot still crashes at *startup*)** | `56da615` |

All 16 are on `main` (HEAD = `56da615`). Believed-safe and staying: #131–#138, #140–#146. #139 is reverted. #142 is harmless. **Next session branches from `main` HEAD and does the `bot/brokers/ibkr.py` refactor above.** The running VPS bot is currently stopped — it boots only after the refactor lands (or temporarily via the `1331fc9` bridge).

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
