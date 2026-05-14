# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-14 — **Architecture pivot.** Patching the IBKR/`ib_async`/`nest_asyncio` stack has failed repeatedly. Bot is DOWN. Next session does the dedicated-event-loop refactor described below — do NOT keep patching.

## ⛔ CURRENT STATE: BOT IS DOWN

- `trading-bot` container was crash-looping with `RuntimeError: cannot enter context: <_contextvars.Context object> is already entered` on every asyncio socket read — the event loop is wedged, no trading, no heartbeat.
- User stopped the container (`docker compose stop trading-bot`). `ib-gateway` left running.
- VPS `main` is at the full-dependency-pin commit (PR #142). The crash happens on that code too — **the bug is NOT dependency drift.**

## Root Cause (confirmed, do not re-investigate)

The `contextvars` re-entry crash is **not** a dependency problem. We proved that: PR #142 pinned the entire 88-package tree to the exact `pip freeze` of the 28h-stable container, rebuilt `--no-cache`, and got the **identical crash**. Dependencies are ruled out.

It is a **code** problem, introduced by today's PRs. The crash is classic `nest_asyncio` + multi-threaded event-loop access: the same asyncio context object gets entered from two places concurrently. The prime suspect is **PR #139 (`28a37a3`, auto-recovery rework)** — it made two changes that together allow multiple background reconnect threads to spawn and drive the same event loop:
1. `_health_check` now calls `_start_background_reconnect()` on fast-path failure.
2. `_reconnect_thread_started` is now reset on thread exit, so the thread can re-spawn repeatedly.

Before #139: at most one reconnect thread, ever, spawned only at startup. After #139: the scheduler's `_health_check` (~5 min cadence) can spawn one repeatedly. Multiple threads each calling `broker.connect()` (asyncio socket I/O under `nest_asyncio`) → concurrent context entry → crash.

This is a SYMPTOM of a deeper fragility: **`nest_asyncio` is a hack**, and the whole codebase calls `ib_async` coroutines from threads with already-running loops (APScheduler jobs, reconnect threads, scalp-monitor callbacks). The contextvars bug has now been fought across PRs #124, #125, #126, #128, #129, #142 — six attempts. Patching does not hold.

## ✅ THE PLAN: dedicated `ib_async` event-loop thread (remove `nest_asyncio`)

This is the actual fix. It is the next session's whole job. Own session, careful work, paper-verify gated.

**Design:**
- Run `ib_async` in ONE dedicated thread that owns ONE event loop, created with `asyncio.new_event_loop()` and `run_forever()`. That loop is never nested, never re-entered.
- Every call into `ib_async` from the rest of the bot (engine loop, APScheduler jobs, reconnect logic, scalp callbacks) goes through `asyncio.run_coroutine_threadsafe(coro, ibkr_loop)` and `.result(timeout=...)` — thread-safe submission to the dedicated loop.
- **Delete `nest_asyncio` entirely** — remove `nest_asyncio.apply()`, drop it from `requirements.txt`. It exists only to paper over the "call async from a sync thread with a running loop" problem; the dedicated-loop pattern solves that properly.
- `bot/brokers/ibkr.py` is the main surface to change. `connect()`, `disconnect()`, `reconnect()`, `get_historical_bars()`, `get_live_price()`, `is_connected()`, streaming subscriptions — all route through the dedicated loop.
- The reconnect logic (engine.py `_start_background_reconnect`, `_health_check`, `reconnect()`) must be reworked so reconnects happen ON the dedicated loop, not in ad-hoc threads. This naturally fixes the PR #139 multi-thread problem too.

**Why this is the right call:** it removes the entire class of bug instead of patching instances. `ib_async`'s own docs/examples use exactly this pattern for multi-threaded apps.

**Manual verify before merge (paper, after market close):**
1. `python -m bot.main --mode paper --no-dashboard` — boots, connects to IBKR, runs 10 min, ZERO `contextvars` tracebacks.
2. Force a gateway drop (`docker compose restart ib-gateway`) — bot detects it, reconnects cleanly via the dedicated loop, no thread storm.
3. Confirm APScheduler jobs (scanner cycle, auto-tune) still reach IBKR without error.
4. Deploy to VPS after close, watch 30 min.

## Fallback if the refactor stalls

If the dedicated-loop refactor proves too big for one session and the bot must trade sooner:
- **`git checkout 1331fc9`** on the VPS — the last commit that ran 28h stable (before ANY of today's 11 PRs) — and rebuild. Abandons today's PRs on the *running* bot but they stay safe in `main`/git history. Guaranteed-known-good code.
- Or just **revert PR #139** (`28a37a3`) and redeploy — keeps the other 10 PRs, removes the prime suspect. Lower-effort than the full refactor, decent odds, but does NOT remove the underlying `nest_asyncio` fragility — it'll bite again.

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
| #142 | **Full 88-package dependency tree pin — did NOT fix the crash** | `2ea9fe3` |

All 11 are on `main`. PRs #131–#137, #141 are believed safe. **#139 is the suspect.** #142 is harmless but didn't help.

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
