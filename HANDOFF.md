# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-15 (post-#156 diagnosis) — **Gate mean_reversion SELL signals on actual position ownership.** Diagnosed live: today's "every signal rejected" had 3 causes — 115 ghost sells (55%), 87 stale signals (41%), 6 chase rejections (3%). Ghost-sell root cause: `mean_reversion._analyze_symbol` SELL branch fired for any scanner-discovered overbought stock regardless of ownership; risk_manager correctly rejected as "No position to exit" but the slot was already burned and the rejection log was drowned in noise.
- `bot/strategies/base.py`: new `set_held_symbols(symbols)` on `BaseStrategy`. `None` default preserves legacy behavior for strategies whose host never plumbs this in.
- `bot/engine.py`: stamps `set(self.positions.keys())` on every strategy before `generate_signals` — both the main scan loop AND the hot-mover fast lane.
- `bot/strategies/mean_reversion.py:228`: SELL branch returns `None` when symbol not in held set.
- `tests/test_mean_reversion_sell_gate.py`: 3 tests (suppressed / emitted / legacy).
- **Out of scope, separate PR worth opening:** `vwap.py:201` and `smc_forever.py:347` also use `action="sell"` but reason field says "SHORT" — mislabeled (should be `action="short"`). Both are 0% allocated → not currently loaded, no live impact. One-line fix per file.
- **Restart context (amplifier, not root cause):** today's `*/5 * * * *` auto-deploy cron restarted the bot 5 times in 40 min (10:55 → 11:35 ET) as PRs #153 → #156 merged in sequence. Each restart wipes cycle counter + bar warmup state. Worth thinking about: rate-limit deploys or pause auto-deploy during RTH.
- **Still unsolved:** the 103s "Stale signal" rejections (87 today). Strategy→risk_manager pipeline has ~1-2 min latency on some signals. Probably scanner blocking or signal queue not draining. Not investigated this session — next priority once the noise drops.
- **VPS auth gotcha discovered this session:** the existing `~/.ssh/github_deploy` key on the VPS is **read-only**. To push from the VPS, a second write-enabled deploy key was added: `~/.ssh/github_deploy_write` + SSH alias `github-write` in `~/.ssh/config`. Remote was switched to `git@github-write:femibol/tpstrategyv3.git`. Keep or revoke per your taste.

2026-05-15 (later-still) — **PR #156: MIDPRICE entries + confidence-scaled + regime-aware sizing.**
- `bot/risk/position_sizer.py`: `calculate()` now takes `confidence` and `regime_multiplier` kwargs. New multiplier stack: `base × Kelly × DD × Session × Confidence × Regime`. Floor 0.25%, ceiling 3% risk (unchanged). Confidence buckets: ≥0.85→1.5x, ≥0.70→1.2x, ≥0.55→1.0x, else 0.7x. Regime clamped to [0.3, 2.0].
- `bot/engine.py:_execute_buy` reads `regime_detector.get_status()` per-signal — **but only applies the multiplier when `confidence > 0.55`**. The SIDEWAYS default lands at 0.5 confidence; without this gate, a "stuck" detector would silently shrink momentum sizing on every entry. With the gate: low-confidence → neutral 1.0x.
- `bot/engine.py:_execute_buy` IBKR path: entries from `daily_trend_rider`, `mean_reversion`, `prebreakout`, `smc_forever` now use MIDPRICE order type during RTH (capped at +0.5% of live price). Speed-critical strategies (momentum_runner, premarket_gap, rvol_*) and all extended-hours orders stay MARKET.
- Open follow-up: regime detector may genuinely be stuck on SIDEWAYS for 2 months (ADX > 25 threshold strict; EMA20/EMA50 spread > 0.5% required). The confidence gate above makes this *safe* — bot doesn't shrink momentum from a non-detection — but the detector itself may need threshold tuning. Diagnostic: `grep -c 'REGIME CHANGE' logs/trading.log`. If zero or near-zero across 2 months, drop ADX threshold to 20 and EMA spread to 0.3% in a follow-up PR.

2026-05-15 (latest) — **PR #155: trades_today counter fix + hot-mover fast lane.** Direct response to live PIII miss: bot tried 3 times (9:50, 10:19, 10:44 AM ET), all rejected for chase / staleness, PIII went on to +121% intraday high. Two structural fixes shipped:
1. **`trades_today` only bumps on filled entry, not on signal generation.** BaseStrategy now exposes `record_entry_filled(symbol)` which the engine calls AFTER a successful position-tracking event. Across 7 strategies (rvol_scalp, momentum_runner, premarket_gap, rvol_momentum, options_momentum, vwap, prebreakout): removed the inline `self.trades_today += 1` and replaced the per-cycle break condition with `if self.trades_today + len(signals) >= self.max_trades_per_day`. The PIII pattern (3 rejected signals burning the daily slots) is closed.
2. **Hot-mover fast lane** in `_main_loop`. Every 3s (alongside `_fast_scalp_monitor`), `_quick_scan_hot_movers` runs momentum-aware strategies (`momentum_runner`, `premarket_gap`, `rvol_momentum`, `daily_trend_rider`) on JUST the top 5 movers. Uses `polygon.get_top_movers` which reads its 15s cache — no extra API calls. Closes the 10s → 3s gap where a 5-15%/min runner used to be evaluated once per 10s and signals went stale before reaching execution.

2026-05-15 (yet later) — **Three remaining follow-ups shipped:**
1. **Deferred-order surfacing** — `engine.py:_execute_signal` now checks `order.get("deferred")` BEFORE the slippage / position-tracking code runs. Previously a queued-by-IBKR order returned with `quantity=requested` (PR #152 had a phantom-position bug for deferred outside-RTH orders that nobody had hit yet). Now: log + return cleanly; fill arrives via streaming when venue opens.
2. **Directional drift check** — `risk_manager.Rule 6` and `engine.py` pre-order slippage are now *asymmetric*. For BUY signals: chase UP (market > signal, trend strengthened) gets the wide cap (5% RTH / 12% extended); chase DOWN (market < signal, setup broke) gets a tight cap (3% RTH / 5% extended). Catches the "buying a fade" pattern that was sneaking through the symmetric check. Added 3 new tests in `tests/test_risk_manager.py`; 62/62 pass.
3. **Strategy time-of-day audit** — quick survey of the other 13 strategies. The 3 with sketchy session awareness (`pairs_trading`, `pead`, `short_squeeze`) are all at 0% allocation in `strategies.yaml`, so no code change needed today. Documented below for when allocation changes.

### Strategy audit (no code changes — for reference)
| Strategy | Allocation | Session | Verdict |
|---|---|---|---|
| mean_reversion | 15% | 24/7 | ✅ Z-score/RSI/BB valid any session |
| momentum | 15% | 24/7 | ✅ EMA/ADX/volume valid any session |
| momentum_runner | 30% | Multi-session | ✅ Has session-aware afternoon reduction |
| rvol_momentum | 10% | Pre-market disabled by RVOL math | ✅ Correct — thin pre-market RVOL is noise |
| rvol_scalp | 5% | 24/7 | ✅ 5% allocation caps damage; risk_manager filters |
| prebreakout | 10% | 24/7 | ✅ Compression patterns form any session |
| premarket_gap | 5% | 4 AM - 10 AM ET (PR #152) | ✅ Sized to settings.yaml window |
| daily_trend_rider | 15% | Multi-session w/ 9 AM ET prescan (PR #152) | ✅ |
| **pairs_trading** | 0% | 24/7 | 🐛 Should be RTH-only if ever enabled (slippage on thin-session legs) |
| **pead** | 0% | 24/7 | 🐛 Should be RTH only + multi-day if enabled |
| **short_squeeze** | 0% | 24/7 | ⚠️ Pre-market entry without SI confirmation is noise |
| options_momentum | 0% | 24/7 | ⚠️ Options thin pre-market |
| smc_forever | 0% | Likely time-gated | ✅ |
| vwap_scalp | 0% | 24/7 | ⚠️ VWAP math degrades pre-market |

**Answer to "do we catch RTH trades?": YES.** 7 of 8 active strategies fire during RTH. None of the recent PRs accidentally tightened the RTH path; PR #153 + this one actually loosened it (engine pre-order RTH 0.8% → 5%) and added directional asymmetry.

2026-05-15 (even later) — **Risk manager session-awareness follow-up.** Live VPS logs showed PR #152's pre-market gates never fired because `risk_manager` was rejecting signals *first* on its own hardcoded 60s staleness and 5% deviation caps. Follow-up PR: signals stamped with `_extended_hours`, risk_manager widens to 180s / 12% during pre/post market, engine pre-order check aligned to use distinct `max_signal_deviation_pct` (5% RTH, 12% extended) so it doesn't become the new binding constraint. `max_slippage_pct` 0.8% stays — it's a different check (post-fill R:R protection).

2026-05-15 (later) — **Pre-market profit recovery + trend rider polish.** 12 fixes landed on `claude/resume-work-AvsjR` from the senior-engineer review (scanning / entry / exit / pre-market). Compile-clean, 59/59 tests still pass. See "Shipping now" below.

Previous: **Architecture pivot: execution is now IBKR-direct, TradersPost disabled.** The "TradersPost not working" symptom unravelled into two real bugs (below). Bot is verified up on the VPS: `Connected to IBKR (PAPER) at 127.0.0.1:4002`, `using IBKR as sole broker`, `IBKR streaming active for 95 symbols`, `0` `cannot enter context` errors. A manual test trade ran the full `handle_manual_signal → IBKR` path cleanly (rejected only by legit risk checks).

## Shipping now (PR pending on this branch)

**Pre-market entry recovery — the gates were filtering out exactly what they were meant to catch:**
1. `engine.py` — pre-order slippage now session-aware (0.8% RTH, `max_signal_deviation_pct=2.5%` outside RTH). Wires the previously dead `max_signal_deviation_pct` config.
2. `engine.py` — spread gate scales by session (2x outside RTH) and price tier (1.5x sub-$5). Was rejecting low-float runners with normal-for-them 3-4% spreads.
3. `ibkr.py` — fill timeout is 90s entry / 120s exit outside RTH (was 15s / 30s for everything). Also: orders left in `PreSubmitted` outside RTH are NOT cancelled — IBKR has accepted them and queued them for next session. Returns `deferred=True` in the order dict so the engine can route it without the misleading "NO EXECUTION PATH" error.
4. `engine.py` — falling-knife guard fails OPEN when in pre/post-market or when signal source is `premarket_gap` / `rvol_momentum` / `momentum_runner`. Was silently killing premarket entries on a data race (scanner already proved direction; the FAIL-CLOSED branch was structurally wrong for these sources).
5. `premarket_gap.py` — `start_hour` default 6→4. Strategy was muting itself for the first 2 hours of premarket while the bot's `_in_premarket` window opens at 4 AM (`settings.yaml:163`).

**Daily trend rider (15% allocation, was leaking trades):**
6. `daily_trend_rider.py` — third entry type `market_qualified`: enters at market when the daily setup is qualified, price is within 2% of today's high, vol ≥ 1x. Previously the bot would qualify a runner, see the breakout already 1.5% extended, and never enter — missing every clean trend day.
7. `daily_trend_rider.py` — risk filter now scales with the stock's own daily ATR (floor 6%, ceiling 10%) instead of a flat 6% cap. The 6% cap was filtering out high-ATR leaders like NVDA/PLTR class — exactly the names that run.
8. `engine.py` — scheduled `_run_trend_rider_prescan` at 9:00 AM ET so candidates are queued before the bell instead of mid-morning when the breakout entries are already extended past the 1.5% gate.
9. `engine.py` — `_check_trend_rider_sharp_drop` intraday exit (3% drop in 30 min). The daily-bar exits only fire at close; this catches institutional distribution mid-session before the trail eats 4-5% off the peak. Wired into `_monitor_positions`.
10. `engine.py` — bad-news threshold for trend riders lowered from severity ≥2 to ≥1. Trend-rider thesis is explicitly "ride till bad news" — an analyst downgrade should at least tighten the trail.
11. `daily_trend_rider.py` — `_score_setup` adds a 52-week-high proximity bonus (0-25 pts). Stocks at 52w highs have no overhead supply and run cleaner; tilts rotation toward genuine breakouts.
12. `strategies.yaml` — `min_green_days: 2 → 3`. Lowered to 2 for paper-mode looseness; 3 is the right live setting.

**Open follow-ups (not in this PR, but worth tracking):**
- `engine.py`'s `_execute_signal` path still doesn't surface the new `deferred=True` order status — the misleading "NO EXECUTION PATH AVAILABLE" message at ~line 4334 will still fire if the order returns deferred. Add: if `order.get("deferred"): log.info(...) ; return ;` before the no-execution-path branch.
- Strategy-by-strategy review of the other 13 strategies in `bot/strategies/` for similar time-of-day / session-awareness issues.
- Verify on real logs which gate is firing most: `grep -E "PRE-ORDER REJECT|SPREAD REJECT|FALLING KNIFE|NOT FILLED" logs/trading.log | awk '{print $NF}' | sort | uniq -c | sort -rn`. Highest-count gate is the one to keep tuning.

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
