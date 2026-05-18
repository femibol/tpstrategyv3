# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-18 (early UTC, session 5 (5)) — **Trade-review improvements shipped (`41b2776`) + every session-5 fix now verified live.** Trade review of 29 deduped logical crypto trades (+$36.26, 33% win, 3 outsized winners carrying it all) surfaced: (1) `momentum` strategy is 1/9 wins (11%) on crypto — disabled it for crypto in `generate_signals` (still gets crypto in `_dynamic_symbols` for `mean_reversion`'s parallel use); (2) `mean_reversion` SELL threshold tightened to z>=1.5 for crypto (was z>=1.0) — z=1.0 was firing on chop and pinging back near break-even; (3) added 5-min min-hold on mean_reversion's own SELL signal for crypto via new `set_held_symbols(symbols, entry_times=...)` kwarg on BaseStrategy. Engine passes `positions[sym]["entry_time"]` at all 3 call sites. Stop-loss + TP unchanged. Deployed at 04:05:15 UTC after the 04:00 cron debounced; survives-restart fix held a **second** time (6 positions restored: SUI, AVAX, LINK, DOT, SOL, ICP — one more than the 03:50 restart proved). First live test of the new SELL guards fired at 00:08:39 EDT: mean_reversion SELL on ICP at z=3.12, RSI=100 — 6:18 after entry (clears 5-min hold), z=3.12 (clears z>=1.5). Engine routed via webhook_exit, closed at +$1.41. 7 positions still open, net unrealized +$5.87.

2026-05-18 (early UTC, session 5 (4)) — **Two follow-on bugs caught LIVE by Improvement A; both fixed in `6205589`.** While verifying session 5 (3), the auto-deploy thrashed on the back-to-back HANDOFF.md commits (`0587ae9`, `8b5754f`) — full rebuild + container recreate each time. The 03:35 UTC restart dropped 3 live crypto positions (AVAX 157.42, DOT 1175.58, XRP 867.04) from `self.positions` because `_load_persisted_positions`'s "not found at broker" check uses IBKR's `get_positions()` — which never knows about crypto. The orphan reconciliation walk (Improvement A from `9855406`) fired correctly at 23:35:19 EDT with `CRYPTO RECONCILE: 3 ORPHAN crypto position(s) likely open on TradersPost` + Discord risk_alert — proving its value in exactly the scenario it was designed for. **3 orphans closed manually via webhook** (logIds: `8729d741`, `930a6575`, `e5bac8f7`). Then shipped `6205589`: (1) `_load_persisted_positions` now trusts persisted state for crypto symbols (since TradersPost has no positions API to confirm against, and Improvement A catches the inverse case); (2) `auto-deploy.sh` computes `git diff --name-only LAST_DEPLOYED..HEAD` and skips the rebuild + recreate if every changed file matches `*.md` / `README` / `LICENSE` / `CHANGELOG` / `HANDOFF*` / `docs/*` — `.last-deploy` still gets updated so the next code-bearing commit triggers a normal deploy. Dry-run confirmed the two HANDOFF commits that caused today's incident classify as DOC_ONLY=true.

2026-05-18 (early UTC, session 5 (3)) — **All session-5 fixes verified LIVE.** `9855406` deployed at 03:20:06 UTC. Within 5 minutes of the new code being up: (a) boot reconciliation walk fired and logged `CRYPTO RECONCILE: clean — no broker-side orphans in the last 48h` (proves Improvement A + the signal_log hygiene from session 5 (2) is consistent); (b) zero `SAFETY GATE BLOCK` messages post-deploy where the previous 3 hours had one every 3 seconds (proves Improvement B's crypto cap split is the binding constraint); (c) two real crypto trades opened — `AVAX-USD 157.42 @ $9.20` and `DOT-USD 1175.58 @ $1.232` — both with `STOP FLOOR APPLIED` and `R/R STRETCH` at INFO (proves `dbe19bf` #3); (d) earlier in the session LTC and LINK each got blocked from immediate re-entry post-close with `CRYPTO RE-ENTRY COOLDOWN: ... closed Xs ago (cooldown 600s) — skipping new entry` (proves `dbe19bf` #4). Only `dbe19bf` #1 (rotation excludes crypto) hasn't been exercised yet — needs ≥6 positions.

2026-05-18 (early UTC, session 5 (2)) — **Improvements A + B shipped: boot-time crypto orphan reconciliation + separate crypto/equity daily trade caps.** Single commit `9855406`. Catalyst was discovering the SUI/ICP orphans from session 5 (1) were just the tip — a signal_log walk over 48h revealed 13 untracked TradersPost crypto positions totalling ~$58K of ungoverned exposure (BTC 0.148, ETH 3.96, XRP 8134, plus 10 others). **All 13 manually closed via webhook (HTTP 200 each, logIds captured)** before this commit landed. Then shipped: (A) `_reconcile_crypto_orphans` at boot — walks `data/signal_log.json` last 48h, surfaces any non-zero positive net qty per crypto symbol as a WARNING + Discord risk_alert; clamps negative net to zero (signal_log over-records exits because TP returns 200 on rejected "no position" exits); also calls `_persist_positions()` immediately after every `self.positions[symbol] = {...}` so a crash before the next 3s scalp tick can't lose an entry; (B) `_gate_global_daily_trade_cap` now bucketed by asset class with separate caps — equity 25, crypto 50 (24/7 market needs the headroom). Earlier today the equity-tuned 25 cap got hit by 18:00 EDT and blocked all crypto for 10 hours. 87/87 tests still pass.

2026-05-18 (early UTC, session 5) — **Crypto churn fixed: rotation no longer rotates out crypto for equity signals, mean_reversion can't immediately re-buy after any close, min_price/max_price no longer reject crypto, STOP TOO CLOSE noise demoted to INFO for crypto.** Single commit `dbe19bf` bundles all four. Surfaced from reviewing today's 39 crypto trades (~$40 reported P&L) and the user's observation that SUI + ICP were still open on TradersPost while the engine reported 0 positions. Orphans closed manually via webhook before the commit landed (SUI 2725.97628, ICP 1101.25468 + 1123.15719, all HTTP 200 with logIds in the commit message). Also confirmed: the multi-row ETC/BCH/AAVE history entries (6 ETC closes for one entry) are legit partial-profit fills via `_partial_close_inner`, not a bug.

2026-05-17 (late UTC, session 4 (2)) — **Two crypto trades shipped + slow-cycle FALLING KNIFE noise silenced + per-broker rate-limit so crypto bursts all fire.** Two more commits after the initial session-4 push: `c237aa9` (falling-knife fail-open now keys on `is_crypto_symbol(symbol)`, not the `_crypto_fast_lane` flag — fixes slow-cycle WARNING noise that the 7c04107 fix didn't cover), `397c78e` (`GLOBAL_MIN_INTERVAL` is now per-instance via `min_interval_override`; crypto broker set to 0, so 3-5 simultaneous fast-lane approvals all fire instead of N-1 dropping with `NO EXECUTION PATH AVAILABLE`). Second live trade landed at 17:50:04: `TradersPost SUBMITTED: ETH-USD qty=1.32102 @ $2,192` (SL $2148.87 / TP $2280.43). 87/87 tests still pass.

2026-05-17 (late UTC, session 4) — **FIRST AUTONOMOUS CRYPTO TRADE FIRED.** Three commits this session unblocked the entire crypto path end-to-end: `7c04107` (falling-knife bypass), `07f4a3f` (crypto pinning in dynamic-symbols cap — the actual root cause of `no_data=45` heartbeats), `d3e2d75` (separate IBKR mirror from crypto). Live validation at 17:33:01 UTC: `TradersPost SUBMITTED: BTC-USD qty=0.03693 @ $78,439` via the CRYPTO webhook (HTTP 200), full SL/TP set, momentum-strategy entry. Also: 87/87 tests pass after fixing the long-broken `test_ibkr_outside_rth_cancel_policy.py` fixture (`_FakeContract` was missing `(exchange, currency)` positional args, and the test asserted `queued` when the broker actually returns `deferred`).

2026-05-16 (late UTC, session 3) — **Crypto pipeline complete: 45-name universe on Binance.US real-time bars, fast lane firing every 3s, fractional sizing through risk_manager, truthful heartbeat — and all of it validated live as `universe=45 | neutral=45` at 11:50:53 ET.** Four commits since the prior handoff: `75789b9` (universe 3→46 + fast-lane reads config + bucketed heartbeat), `8bb89bf` (Binance.US adapter primary, Yahoo fallback for MKR/TON, MATIC→POL + RNDR→RENDER alias map, STX dropped), `108cb91` (heartbeat WAIT verdict bucketed as no_data, `LOG_LEVEL` env var so future sessions aren't blind to `log.debug` like this one was). Bot is now in the "waiting for a real signal" state for the first time — pipeline works end-to-end, market is just quiet on a Saturday afternoon.

### `9855406` — boot-time crypto reconciliation + separate trade caps

User asked for Improvements A and B from the session-5 review. While drafting A, a signal_log walk uncovered far more orphans than the SUI/ICP I'd already closed — 13 total across BTC/ETH/XRP/AVAX/FIL/ICP/LINK/LTC/NEAR/SUI plus DOT and residuals on NEAR/LINK from older partial-close sequences. Approximate notional at entry was ~$58K of ungoverned TradersPost exposure. User authorized closing all of them; webhook curls returned HTTP 200 for each with logIds captured in the commit body.

**A. Boot-time reconciliation (`_reconcile_crypto_orphans` at `bot/engine.py:660`).** Walks `data/signal_log.json` over the last 48h, nets buy webhooks against exit webhooks per crypto symbol (filtering on `success=true` and `status_code<300`), and surfaces any non-zero *positive* net qty as an orphan with `log.warning` + `notifier.risk_alert`. Negative net is clamped to zero — signal_log over-records exits because TradersPost returns HTTP 200 on "exit signal accepted" even when the broker rejects "no position to close", and you can't be short on a spot subscription. Why this approach: TradersPost crypto subscriptions are webhook-only — no REST endpoint to query open broker positions like the IBKR path does for equity. The signal_log walk is a strong tripwire even if it can't be a true reconciliation. Runs once at boot from `_init_broker_and_sync` after `_load_persisted_positions` completes.

**A (companion fix): `_persist_positions()` now called immediately after entry.** `bot/engine.py:5250`. Previously persist only ran every 3s from the `_fast_scalp_monitor` tick — if the bot crashed between an entry add and the next scalp tick, the new position was lost. Confirmed root cause of the SUI/ICP loss across the 19:40 EDT restart on 2026-05-17.

**B. Asset-class-bucketed daily trade cap (`_gate_global_daily_trade_cap` at `bot/engine.py:4188`).** Now takes a `symbol` argument; counts today's entries from `trade_history` + open `positions` bucketed by `_is_crypto_symbol`; checks the right cap based on which bucket the new signal belongs to. Defaults: equity 25 (unchanged), crypto 50 (new — `max_total_crypto_trades_per_day` in `config/settings.yaml:41`). Discord alert key is per-bucket (`_daily_cap_alerted_{bucket}_{date}`) so a chatty crypto cap doesn't suppress an equity-side notification. The old single 25-cap was the binding constraint on crypto every evening this week — bot hit 37/25 by ~6 PM EDT and locked crypto out for the remaining 10 hours.

**Session 5 (2) orphan cleanup (HTTP 200 each):**
- First curl (with session 5 (1) push): SUI 2725.97628 (`0c5a7713`), ICP 1101.25468 (`a62b7149`), ICP 1123.15719 (`4df84acd`)
- 10-orphan batch (this session): AVAX 312.08349 (`f60cedcc`), BTC 0.1477 (`2df8f71e`), ETH 3.96378 (`2da9ba4e`), FIL 2972.48311 (`acc423c7`), ICP 2224.41187 (`622caa10`), LINK 596.78732 (`2f8c8cc7`), LTC 51.69074 (`44849994`), NEAR 1920.83714 (`4aa4269b`), SUI 2725.97628 (`d488ad52`), XRP 8134.29488 (`02cf18f6`)
- 3 residuals after extending walk to 48h: DOT 1469.47159 (`3c1b87f3`), NEAR 1215.02617 (`7809c18a`), LINK 151.27545 (`cba3f152`)
- Total 16 webhook exits across two sessions — all HTTP 200. Per-symbol `RATE_LIMIT_MAX=3 / 60s` capped some flows, so the 10-batch used 4s spacing.

**signal_log.json hygiene:** appended the manual-cleanup exits with `strategy="manual_orphan_cleanup"` so the reconcile walk wouldn't false-positive on the next bot restart. Deduped the SUI (2x same qty) and pruned the ICP 1101+1123 entries (the 2224 entry from the 10-batch already covers both). After the negative-net clamp the walk should now report `CRYPTO RECONCILE: clean — no broker-side orphans` on next boot.

**Live state at handoff time (session 5 (2)):**
- Bot still on `dbe19bf` (auto-deploy due ~03:00 UTC for `9855406`).
- Container restarted 02:50:14 UTC. Engine has 0 positions per `/api/positions`.
- TradersPost crypto subscription has 0 open positions (16 webhook exits confirmed HTTP 200).
- Scheduled wake-up at 03:56 UTC to catch the first post-04:00 crypto-cap-reset trade execution and verify `dbe19bf` fixes #1/#3/#4 trigger live.

### `dbe19bf` — four crypto fixes from the session-5 trade review

Triggered by user reporting "Sui and icp are still open. review each trade and improve." Engine had 0 positions per `/api/positions` and `positions_state.json` (stale, last write 00:56 UTC); TradersPost CRYPTO subscription had residual SUI + ICP from re-entries that the engine had lost track of. Trade review surfaced four overlapping bugs all in one commit.

1. **`_momentum_rotation_check` excludes crypto.** `bot/engine.py:7110-7120` — crypto positions are skipped in the scoring loop, so they can never be the "weakest" candidate to rotate out. Equity rotation was killing crypto slots ("Momentum rotation: replaced by stronger signal MLGO") to make room for equity signals, then mean_reversion immediately re-fired the same crypto buy on the 3s fast lane. Observed live 2026-05-17 18:09-18:46 EDT: SUI/ICP/LINK each entered+rotated 3x in ~30 min, leaving orphans when the cycle landed on a re-entry with no follow-up close. Crypto routes through `tp_crypto_broker` (separate subscription, separate venues), so closing a crypto slot doesn't free capacity on the equity broker that rejected_signals are targeting anyway.

2. **`risk_manager` min_price/max_price skip crypto.** `bot/risk/manager.py:140-156` adds `_is_crypto_sym` bypass next to the existing `asset_type != "option"` bypass. The $0.50 floor was rejecting MATIC ($0.09), FLOKI/PEPE/BONK/SHIB every 3s on the fast lane — log was full of `REJECTED: buy MATIC-USD | Price $0.09 below minimum $0.5`. Crypto sizing is bounded by `crypto_max_position_pct` (10%), not by nominal price; the rule has no asset-class meaning here.

3. **Crypto-aware stop floor + INFO log.** `bot/engine.py:4695-4715` — crypto floor is now 5% (matches `crypto.risk.stop_loss_pct`) instead of 2%, and the log demotes to INFO for crypto. Crypto ATR is tiny relative to price (MATIC ATR ≈ $0.0001 on a $0.09 entry = 0.1% stop), so the near-zero stop is expected, not anomalous. Same treatment for R/R STRETCH at lines 4808-4820.

4. **Crypto re-entry cooldown (10 min) on duplicate-entry guard.** `bot/engine.py:4516-4530` — symmetric to the existing 5-min `_exit_cooldown_secs` (which only blocks re-CLOSES). Without it, any close on a crypto symbol whose Z-score / RSI is still oversold triggers an immediate mean_reversion re-buy, and that re-buy can race with the close's exit-tracking → orphan position on TradersPost. Equities aren't affected; they already have the `broker.get_positions()` IBKR sync check.

**Manual orphan cleanup performed before this commit:**
- `SUI-USD exit qty=2725.97628` → log `0c5a7713-8df3-4469-b54a-e58d6291e485` (HTTP 200)
- `ICP-USD exit qty=1101.25468` → log `a62b7149-da9d-4831-b8b8-3820d041c672`
- `ICP-USD exit qty=1123.15719` → log `4df84acd-6d81-4853-8854-992decda5425`

87/87 tests pass. Deployed via auto-deploy on the next 5-min tick (push at ~02:50 UTC).

### Session-5 trade review snapshot

- 39 closed crypto trades (incl. partial-fill duplicates), reported total +$40.19. Real number is lower after deduping the 5-6 ETC partial-target rows.
- Per-symbol non-trivial loss: INJ-USD -$46.47 on 65m hold (only crypto trade > $20 loss).
- All entries flagged STOP TOO CLOSE + R/R ENFORCE — now silenced for crypto per #3 above. The stops themselves weren't broken; just noisy.
- 39x `exit_reason` field is empty in trade_history.json — `_close_position` writes `reason: <type>` and `reason_detail: <msg>` instead. Display side just hadn't been updated to read either. Worth a follow-up: pick a field name and have the analyzer use it consistently. Open follow-up.
- The ETC pattern (1 entry → 6 history rows) was `_partial_close_inner` calling `trade_analyzer.persist_trade` on every partial profit target. Records have `partial: True` set but analytics views weren't filtering it. Cosmetic, not a real bug.

### Live state at handoff time (session 5)
- Bot on `dbe19bf` (auto-deploy due ~02:55 UTC). Latest log line before push: `22:41:58 EDT — CRYPTO FAST LANE HEARTBEAT (mean_reversion): universe=45 | BUY[1]: MATIC-USD(...) | warming=14 | neutral=27 | no_data=3`.
- Engine has 0 positions per `/api/positions` (AAVE + XRP closed at 21:02:54 EDT with +$45.16 and +$4.03; positions_state.json is stale).
- Orphan crypto on TradersPost is now zero (manual closes confirmed HTTP 200).
- Anthropic API key on the bot is out of credit: `Claude API error 400: ... Your credit balance is too low` — AI insights/post-trade learning are off until topped up. Trading itself is unaffected.

### Open follow-ups (session 5 net-new + carry-over)
- **Persist crypto positions across restart.** SUI/ICP entries at 18:09-18:46 EDT never made it into the post-19:40-restart `positions_state.json`. Either the restart loaded a pre-18:09 snapshot, or the saver was filtering crypto. Worth instrumenting the load + save paths around `data/positions_state.json` to confirm crypto is included.
- **`exit_reason` field in trade_history.json is empty.** `_close_position` writes `reason` + `reason_detail`; analyzer/reader sites should pick one and use it.
- **Carry-over (session 4 (2)):** Yahoo crypto path HTTP 429, `api.binance.us` IPv6-only, per-symbol cap (`RATE_LIMIT_MAX=3/60s`) is the crypto-broker floor now that `min_interval=0`, DNS/netns gotcha, `max_price=$500` config ceiling, `vwap.py:201` + `smc_forever.py:347` `action="sell"`→`"short"`. All still un-shipped.

### `397c78e` — per-instance webhook cooldown; crypto broker set to 0

`TradersPostBroker.GLOBAL_MIN_INTERVAL = 3` was a class constant, so every instance shared the same 3-second floor. Crypto fast lane often approves 3-5 signals in the same second (BTC/ETH/SOL/XRP all hit oversold thresholds together), so the first call lands and the rest hit `RATE LIMIT: Global cooldown - 0.5s since last webhook` → `TradersPost webhook FAILED` → `NO EXECUTION PATH AVAILABLE — cannot execute BUY ...`. Observed at 17:33:02 (XRP/ETH/SOL dropped after BTC) and 17:50:04 (SOL dropped after ETH).

Fix: `TradersPostBroker.__init__` now takes a `min_interval_override` kwarg and stores `self.min_interval` per-instance; the rate-limit check uses `self.min_interval` instead of the class constant. `bot/engine.py:295` constructs `tp_crypto_broker` with `min_interval_override=0`. The per-symbol cap (`RATE_LIMIT_MAX=3` signals per 60s) stays in place as the runaway-loop floor — a single ticker can't fire more than every ~20s on average. Equity broker and mirror keep the 3s default unchanged.

### `c237aa9` — falling-knife fail-open keys on symbol type, not flag

Side-effect of the 07f4a3f pinning fix that the 7c04107 falling-knife fix didn't cover: now that crypto is permanently in `momentum`/`mean_reversion`'s dynamic universes, the SLOW cycle also emits crypto signals (e.g. momentum sees ETH-USD on a pullback). The 7c04107 fail-open check gated on `signal.get("_crypto_fast_lane")` — only set by `_quick_scan_crypto`. Slow-cycle crypto signals lacked the flag and hit the fail-CLOSED `FALLING KNIFE BLOCK (no quote)` branch. The fast lane fired the same signal 3s later so trades still completed, but the WARNING noise + redundant rejection cycle was real.

Fix at `bot/engine.py:4445`: `is_crypto = self._is_crypto_symbol(symbol)`. Asset class is what determines whether `get_quote()` can possibly return a useful value, not which code path raised the signal. Live confirmation 17:50:00–17:50:04: slow cycle blocked ETH-USD (WARNING), fast lane approved 3s later, TradersPost SUBMITTED filled — the warning is now silenced going forward.

### `07f4a3f` — crypto pinning against dynamic-symbols cap (the actual blocker)

The hidden bug that ate session 3's "pipeline works" claim: `mean_reversion` and `momentum` cap `_dynamic_symbols` at 50 with a plain newest-wins eviction. `_discover_dynamic_symbols` injects crypto FIRST, then equity discovery runs — so equity timestamps are always strictly newer, and the cap silently dropped the entire crypto universe every cycle. `_bars_cache` stayed empty for crypto → `mean_reversion._analyze_symbol`'s no-bars early-return wrote `{"status":"no_data","verdict":"WAIT"}` for all 45 symbols → heartbeat sat on `universe=45 | no_data=45` for 18+ hours.

The diagnostic that nailed it: temporary `log.info(f"DIAG strat {sname}: get_symbols={len(syms)}, crypto_in={...}, dyn_attr={len(...)}")` inside `_update_data`'s iteration. Result: `DIAG strat mean_reversion: get_symbols=50, crypto_in=0, dyn_attr=50` immediately after a `CRYPTO INJECT: 45 symbols (...) → mean_reversion(45 dyn), momentum(45 dyn)` log line. 45 in, 0 out — the cap had already evicted them by the next equity-discovery cycle.

Fix: in `bot/strategies/mean_reversion.py:54-83` and `bot/strategies/momentum.py:48-77`, the cap-eviction branch now unconditionally keeps any symbol ending in `-USD`/`-USDT`/`-BTC`/`-ETH` and only applies the cap to non-crypto entries. The crypto universe is bounded by config (45 names) so pinning can't blow up the dynamic set. Validated live: 16 min after deploy the heartbeat showed `BUY[4]: ADA-USD(z=-1.24 rsi=18.0), FLOKI-USD..., HBAR-USD..., XLM-USD... | warming=11 | neutral=17 | no_data=13` — real z-scores and RSI across the universe.

### `7c04107` — crypto fast lane: bypass falling-knife "no quote" block

Independent of the pinning bug. Once crypto bars DID flow earlier today (11:20–12:42 ET, before IBKR disconnected and reconnect re-shuffled the dynamic set), every approved crypto buy was killed by the falling-knife guard's fail-CLOSED no-quote branch. Crypto has no IBKR streaming quote source — `market_data.get_quote()` only knows about equity sources, so it always returned None for crypto and the gate blocked the entry as a "precaution." Pattern in logs 12:38:09–12:38:21 ET: back-to-back `APPROVED: buy BCH-USD` immediately followed by `FALLING KNIFE BLOCK (no quote): BCH-USD — cannot verify day change, blocking entry as precaution`, repeating every 3s for ATOM/INJ/BCH all session.

Fix at `bot/engine.py:4440-4451`: same shape as the PR #160 manual-signal fix. Add `is_crypto = bool(signal.get("_crypto_fast_lane"))` to the `fail_open` set. The legit "quote present + day_change ≤ threshold" branch above still fires for crypto if a quote ever does materialize, so the protection isn't lost — just stops fail-closing when the quote source doesn't exist.

### `d3e2d75` — IBKR mirror skips crypto entries/exits

User caught immediately after the first crypto trade landed: `TP MIRROR: BUY BTC-USD qty=0.03693 ... → primary webhook (200)`. The `tp_mirror` instance is wired to `TRADERSPOST_MIRROR_WEBHOOK_URL` whose subscription visualizes IBKR/equity fills, so mirroring crypto there cross-contaminates the equity book with a phantom crypto position. Crypto already goes through `tp_crypto_broker` (separate TradersPost subscription on crypto venues), so the mirror call is pure duplication.

Fix: both `tp_mirror.notify_trade()` call sites (`bot/engine.py:5213` entry, `bot/engine.py:5477` close) gate on `not self._is_crypto_symbol(symbol)`. Equities still mirror to the IBKR visualization webhook unchanged.

**Manual cleanup performed:** the 17:33:01 BTC-USD trade leaked into the IBKR mirror before this fix landed. Curled the exit directly to the mirror webhook (`{"ticker":"BTC-USD","action":"exit","quantity":0.03693}` → HTTP 200, log ID `3573d50b-bcc7-4a43-aa1d-db0db545eb1d`) and user confirmed the position closed on the TradersPost dashboard. No standing phantom crypto position remains.

### Tests: `tests/test_ibkr_outside_rth_cancel_policy.py` un-broken

The file was untracked + the module errored at collection time (`_FakeContract.__init__() takes from 1 to 2 positional arguments but 4 were given`), blocking all 3 tests in it. Two fixes:
1. `_FakeContract` now accepts `(symbol, exchange, currency, **kw)` matching the real `Stock(symbol, "SMART", "USD")` call shape in `bot/brokers/ibkr.py`.
2. `test_outside_rth_presubmitted_is_not_cancelled` was asserting `result.get("queued") is True` and `quantity == 0`, but the broker actually returns `{"deferred": True, "quantity": <requested>, ...}` — the engine reads `order.get("deferred")` BEFORE position tracking so no phantom position is recorded (HANDOFF PR #152 / followup). Updated the assertions to match the actual contract.

Result: 87/87 pass.

### Live state at handoff time (session 4 (2))
- Bot on `397c78e`, container started 18:01 UTC, healthy.
- `LOG_LEVEL=INFO` (default).
- Heartbeat steady-state: real z-score/RSI values, periodic `CRYPTO FAST LANE: approved buy ...` lines, signals routed to `tp_crypto_broker`. Crypto bursts no longer dropping after the first — next session should see 3-5 `TradersPost SUBMITTED: <SYM>-USD` lines back-to-back where session-4-(1) had 1 SUBMITTED + N rate-limit warnings.
- **Two confirmed fills:** BTC-USD 17:33:01 (0.03693 @ $78,439, user manually closed it on IBKR mirror), ETH-USD 17:50:04 (1.32102 @ $2,192, SL/TP set on the bot side).
- Yahoo crypto path is currently HTTP 429 (rate-limited). Binance.US is the de-facto sole source. MKR-USD and TON-USD (the two names Binance.US doesn't list) will silently no-data until Yahoo's 429 clears — non-blocking for the other 43 names.

### Open follow-ups (carry-over + new)
- **Verify the ETH-USD fill landed.** Same as the BTC-USD verification before it — `TradersPost SUBMITTED` only means the webhook was accepted. Check the CRYPTO TradersPost subscription's order history to confirm ETH actually filled and that BTC isn't somehow there (it should be on the IBKR-mirror history, since the mirror routing pre-dates `d3e2d75`).
- **`api.binance.us` returns IPv6-only addresses** (`2600:9000:...`) and the container has IPv6 routing. If a future deploy lands on a host without v6, Binance.US fetches will fail silently — Yahoo is the only fallback and it's been rate-limited all afternoon. Consider forcing IPv4 with `curl -4` equivalent in `_fetch_binance_us_klines`.
- **Per-symbol cap (`RATE_LIMIT_MAX=3 / 60s`) is the new floor for crypto.** With `min_interval=0` the per-symbol cap is the only thing preventing a runaway. If a single ticker pulses oversold + recovers + oversold again three times in 60s, it'll be blocked. The cap log is still WARNING-level — keep an eye on `RATE LIMIT: <SYM>-USD has 3 signals in last 60s` to see if any name needs a relaxed cap.
- **Carry-over from session 3:** the DNS / netns gotcha (auto-deploy `--force-recreate trading-bot` can drift the bot's netns away from `ib-gateway`, killing all external DNS), the `max_price=$500` ceiling, `vwap.py:201` + `smc_forever.py:347` `action="sell"`→`"short"`. All still un-shipped.

2026-05-16 (late UTC, session 2) — **`c26dad3`: three real blockers behind "no organic crypto trade".** (1) DNS broken inside `trading-bot` container — netns linkage to `ib-gateway` had drifted (different `net:` inodes despite `network_mode: "service:ib-gateway"`); fixed by `docker compose down && up -d` rather than per-service recreate. (2) `risk_manager` Rule 7 was rejecting every BTC signal at "Position $77962 exceeds max $3000" — `signal.quantity` was defaulting to 1 → 1 BTC notional vs the 10%-of-balance crypto cap. (3) `position_sizer.calculate` returned 0 shares for BTC (`math.floor(3000/77000)`); added a crypto branch that keeps quantity as a float quantized to 5 decimals. Plus: mean_reversion heartbeat verdict now mirrors the real `entry_ready` path (it was saying "BUY SIGNAL" for 30+ minutes while the real entry waited on a green reversal candle). **Validated live during the same session** — heartbeats showed `universe=46 | neutral=46` post-deploy with all 46 crypto symbols loaded.

2026-05-16 (mid UTC) — **Crypto follow-up shipped: looser mean_reversion thresholds + crypto fast lane + auto-deploy `HEAD ≠ deployed SHA` fix.** All three commits manually deployed at 07:42:53 UTC after the VPS-push gotcha (below) blocked auto-deploy. Container healthy on `05ca7b5` (or newer if this commit landed); fast lane wired and silent (logs only on signal approval).

### `108cb91` — heartbeat truthfulness + `LOG_LEVEL` env var

Two follow-ups that came out of session-3 debugging.

1. **Heartbeat `WAIT` was masquerading as `NEUTRAL`.** The new bucketed heartbeat from `75789b9` had `verdict=="WAIT"` falling through to the `else: neutral` arm at `engine.py:2129`. mean_reversion sets `verdict="WAIT"` when bars haven't loaded (`{"status":"no_data","verdict":"WAIT"}`), so a freshly-booted bot with zero crypto bars would heartbeat `universe=45 | neutral=45` and look healthy. New explicit `elif verdict == "WAIT"` branch bumps `no_data` instead.

2. **`LOG_LEVEL` env var support.** `bot/utils/logger.py:24` was hardcoded to set the logger level to INFO regardless of the file handler being DEBUG — which means every `log.debug(...)` was being dropped at the logger level before reaching any handler. This wasted ~15 min of session-3 debugging trying to find Yahoo/Binance fetch failures that weren't actually being logged. Now `setup_logger` reads `LOG_LEVEL` env (defaults to INFO). To debug a future bar-fetch issue: set `LOG_LEVEL=DEBUG` in `.env`, recreate the container, the file gets the firehose. Console handler still pinned to INFO so foreground stays readable. `docker-compose.yml` updated to thread the env var through.

### `8bb89bf` — crypto data: Binance.US primary, Yahoo fallback

Previous crypto bar source was Yahoo-direct, which silently returned 0 bars for ~22% of the new universe (PEPE, APT, MATIC, RNDR, SUI, WIF, BONK, FLOKI, JUP, SEI) and got them stuck in the `_bars_fail_cache` 120s loop. Plus Yahoo crypto data is ~5s delayed.

New `_fetch_bars` flow for crypto symbols (`bot/data/market_data.py:249-273`):
1. **Binance.US klines API** — real-time, ~60ms median, no auth, 1200 req/min budget. Covers 43/45 of the universe. The `_BINANCE_ALIASES` dict translates rebrands at the API boundary (`MATIC → POL`, `RNDR → RENDER`) so the universe yaml keeps the common ticker names. Tries `{base}USDT` first, then `{base}USD`, then `{base}BUSD` so we don't have to per-symbol-configure the quote currency.
2. **Yahoo Finance direct** — fallback for MKR-USD and TON-USD (Binance.US doesn't list them).
3. **None** — let the bar-fail cache backoff kick in.

`binance.com` is geo-blocked from the Linode block this VPS runs on (HTTP 451); `api.binance.us` works fine. End-to-end test from inside the container before deploy: **45/45 symbols loaded, 0 failures, ~9s total** for full universe fetch. Live post-deploy verification: `universe=45 | neutral=45` at 11:50:53 ET.

### `75789b9` — crypto universe 3 → 46 + fast lane reads from config

Two changes that go together. The user wanted "the crypto universe" — concerned a random altcoin going parabolic would slip past a 3-name list.

1. **`config/settings.yaml`** — `crypto.symbols` grew from `[BTC, ETH, SOL]` to a 46-name "fat list" covering L1s, L2s, DeFi, memes, AI/RWA. Risk is bounded by `max_crypto_positions` and the 10% crypto position-size cap, not by the symbol count, so growing the list doesn't increase capital exposure. (Later trimmed to 45 in `8bb89bf` after STX was found unsupported by both data sources.)

2. **`bot/engine.py:2087`** — `_quick_scan_crypto`'s crypto symbol set was hardcoded to `("BTC-USD", "ETH-USD", "SOL-USD")`. **Even after expanding the yaml the fast lane would have stayed at 3** — silent universe gap. Now reads from `self.config.settings["crypto"]["symbols"]`.

3. **`engine.py:2103` heartbeat reformat** — per-symbol rows × 46 would be a 5KB log line every 60s. New format buckets by verdict and only spells out `BUY SIGNAL` + `WAIT:*` near-misses; collapses NEUTRAL / WARMING UP / no_data into counts:
   ```
   CRYPTO FAST LANE HEARTBEAT: universe=46 | BUY[2]: SOL-USD(z=-1.1 rsi=38 bb=MIDDLE), AVAX-USD(...)
                                | WAIT[3]: ETH-USD(needs green bar), ...
                                | warming=8 | neutral=33
   ```

**Open follow-up (next session):** replace the static yaml list with a CoinGecko `/coins/markets?order=volume_desc&per_page=100` hot-movers lane that injects symbols into the same `_fetch_bars` path (no second adapter needed thanks to `8bb89bf`). Mirror of the equity hot-mover pattern at `_quick_scan_hot_movers`.

### `c26dad3` — crypto: fractional sizing + truthful heartbeat verdict

Three independent reasons "no organic crypto trade has fired" despite the fast lane being wired up + active for ~14h:

1. **`risk_manager` Rule 7 (`bot/risk/manager.py:184–202`)** was rejecting every BTC signal at "Position $77962 exceeds max $3000". `mean_reversion`'s signal dict has no `quantity` field, so `signal.get("quantity", 1)` defaulted to **1 BTC = $77K** vs the 10%-of-balance crypto cap. Fix: when `signal.quantity` is missing AND the symbol is crypto, set `position_value = max_position` (true-by-construction — the downstream sizer is guaranteed not to exceed it). Concrete proof from log: at 09:04 UTC we logged 10 back-to-back `REJECTED: buy BTC-USD | Position $77962 exceeds max $3000` rejections in 30 seconds.

2. **`position_sizer.calculate` (`bot/risk/position_sizer.py:280+`)** would have returned 0 shares for BTC anyway: `math.floor(3000 / 77000) = 0`. Added an early crypto branch that keeps quantity as a float quantized to 5 decimals (the precision TradersPost's crypto subscriptions accept), with a $10 dust filter. Returns `0.03896` for a $3K cap on $77K BTC, etc. Non-crypto integer path unchanged.

3. **mean_reversion heartbeat verdict was lying.** Old code: `verdict = "BUY SIGNAL"` iff 2 of {zscore_ok, rsi_oversold, at_lower_bb} passed. Real entry path (`buy_signal` block at ~line 199) additionally requires a green/doji *reversal candle* and, for some paths, `vol_ratio > 1.3`. Observed today: heartbeat said `BTC-USD verdict=BUY SIGNAL` for 30+ consecutive minutes (10:20–10:59 ET) with zero signals fired. Fix: compute `reversal_candle` + an `entry_ready` boolean BEFORE the verdict block, then the buy_signal branch reuses `entry_ready` (single source of truth). New verdict labels for the "close but not firing" cases: `WAIT: needs green bar`, `WAIT: needs vol>1.3x`, `WAIT: combo mismatch`.

### DNS / netns gotcha (worth a follow-up — not solved structurally)

**The actual reason the heartbeat was stuck on `z=-1.47 rsi=36.2` for 40+ minutes:** DNS was broken inside the `trading-bot` container. `getent hosts query1.finance.yahoo.com` returned empty → every `_fetch_yahoo_direct(BTC-USD/...)` call dropped into the `_bars_fail_cache` 120s-backoff loop forever → the bars in `_bars_cache` were frozen at boot-time data (hours old).

Root cause: `docker exec ... readlink /proc/self/ns/net` showed the two containers in **different netns inodes** (`4026532448` for gateway, `4026532627` for bot) despite `docker-compose.yml` declaring `network_mode: "service:ib-gateway"`. Most likely a per-service `--force-recreate` (auto-deploy does this) restarted the bot while leaving its `container:<id>` link pointing at a now-dead gateway container, and the kernel handed it an empty netns instead of erroring. After a clean `docker compose down && up -d` the inode matched (`4026532506` for both) and DNS resolved instantly.

**Tried + rejected:** adding `dns: [8.8.8.8, 1.1.1.1]` to the `trading-bot` service in `docker-compose.yml`. Docker rejects this combination with `conflicting options: dns and the network mode` because `network_mode: service:...` requires inheriting the target's resolv.conf. The bot must rely on `ib-gateway`'s DNS pins.

**Open structural fix (NOT in this PR):** auto-deploy's `docker compose up -d --force-recreate trading-bot` should either also recreate `ib-gateway`, or `auto-deploy.sh` should verify `readlink /proc/self/ns/net` matches between the two containers post-deploy and re-link if not. Right now this can silently break the bot's external connectivity any time `ib-gateway` restarts between `trading-bot` deploys (DNS, Yahoo crypto bars, Discord notifications, all dead — only IBKR still works because that's 127.0.0.1 inside the shared netns, which the kernel resolves locally regardless).

### What the next session should expect to see

The fixes are committed (`c26dad3`) and pushed; the container has been manually recreated. First post-boot heartbeats (11:11–11:12 ET) still showed `<no scan_results>` / `verdict=WAIT (no_data)` because `_update_data`'s first cycle hadn't yet fetched crypto bars when this handoff was written. By the time you read this, expect one of two outcomes:

- **Healthy:** heartbeats show changing z-score/RSI values across consecutive minutes, with verdict cycling between `NEUTRAL` / `WARMING UP` / `WAIT: needs green bar` / `BUY SIGNAL`. If a `BUY SIGNAL` lands, look for `Position size (crypto): 0.0389 BTC-USD @ $77962.00 = $3,033.21` from the sizer, then `CRYPTO FAST LANE: approved buy BTC-USD ...`, then `TradersPost SUBMITTED: BTC-USD qty=0.0389`.

- **Still broken (DNS again):** heartbeats stuck on identical z-score/RSI values for many minutes. Reproduce with `docker exec trading-bot-trading-bot-1 getent hosts query1.finance.yahoo.com` — empty means netns drifted again. Fix: `docker compose down && docker compose up -d` (not `up -d --force-recreate trading-bot` alone).

### `cb11360` — `mean_reversion` crypto thresholds loosened
After 14h of 24/7 BTC/ETH/SOL injection with code defaults (`entry_zscore_crypto=-1.2`, `rsi_oversold_crypto=45`) producing zero `mean_reversion` fires, dropped overrides into `config/strategies.yaml`:
- `entry_zscore_crypto: -1.0` (was -1.2)
- `rsi_oversold_crypto: 40` (was 45)
- `rsi_overbought_crypto: 55` (left at code default; no inflated-short noise to justify symmetric move yet)

### `05ca7b5` — crypto fast lane (`_quick_scan_crypto`)
Mirrors `_quick_scan_hot_movers`. Runs every 3s alongside `_fast_scalp_monitor` + the hot-mover lane. Narrows `mean_reversion` + `momentum`'s `_dynamic_symbols` to `{BTC-USD, ETH-USD, SOL-USD} - held`, runs `generate_signals`, stamps `timestamp`/`market_price`/`_extended_hours`/`_fast_lane=True`/`_crypto_fast_lane=True`, filters via `risk_manager`, pushes straight to `_execute_signal`. No RTH gate (crypto is 24/7). Bar data reused from the last slow cycle — Yahoo crypto fetches are exempt from the 20/cycle IBKR equity budget, so freshness matches what `_run_strategies` sees. The win is purely in evaluation cadence: the slow ~132s cycle that overwrote the 02:05 ET ETH signal in its 02:07 batch can no longer happen for crypto.

### `_update_data` profile (static analysis, no instrumentation needed)
`market_data.py:181` caps non-crypto bar fetches at `max_bar_fetches_per_cycle=20`. With IBKR pacing ~5s/fetch, that's the 102.5s. Crypto fetches bypass the budget (`market_data.py:213-219`) and run ~200ms each on Yahoo — they contribute ~600ms to `_update_data`, not the 102s. So crypto bar freshness was NEVER the cycle-time blocker; the ageing-out symptom was purely a strategy-evaluation cadence problem, which the fast lane fixes structurally. The 20-fetch equity budget can be lowered to 10 (cycle ~50s) or replaced with bulk Polygon aggregates later if equity-side cycle time matters more.

### Auto-deploy `HEAD ≠ deployed SHA` fix (this commit)
**Gotcha:** the `62c7d17` topology check correctly handles `BEHIND>0` and `AHEAD>0`, but missed the case where a session running on the VPS itself makes a commit + pushes — LOCAL=REMOTE the instant after push, so BEHIND=0 and the script logs `No changes detected` forever, while the running container is still on the older image. We hit this end-to-end this session — the 07:40 UTC cron tick saw `cb11360` + `05ca7b5` already on origin/main, BEHIND=0, AHEAD=0, exited cleanly. Had to manually `docker compose build && up -d --force-recreate` at 07:42:53.

**Fix:** `.last-deploy` body is now `git rev-parse HEAD` of what was actually built (was just `touch`ed). The script reads it as `LAST_DEPLOYED_SHA` and adds a third deploy trigger: if `BEHIND=0 AND AHEAD=0 AND HEAD ≠ LAST_DEPLOYED_SHA` → deploy. Backward-compatible: empty body reads as "" → don't second-guess (won't blind-deploy on first run). Seeded `.last-deploy` with `05ca7b5` so this commit will be picked up by the first post-merge tick.

### Stale-doc cleanup
The earlier HANDOFF note "Auto-deploy also doesn't pass `--build`, so code-only changes don't actually reach the running image..." was true at the time but is now stale. Current `deploy/auto-deploy.sh:124` calls `docker compose build trading-bot --quiet` before `up -d --force-recreate`, and the inline comment explicitly explains why ("Python source is BAKED INTO the image at build time"). The note can be removed from any local notes.

### Open follow-ups
- **First organic CRYPTO FAST LANE: log line.** The fast lane only logs on signal approval. Silent so far (~1 min of post-restart runtime when this was written, plus crypto is quiet at 03:43 ET). Worth grepping `logs/trading.log` for `CRYPTO FAST LANE:` after the next Asian/European session to confirm both the looser thresholds AND the 3s cadence have produced fires.
- **Cycle-time relief for equities** (if anyone asks). Drop `data.max_bar_fetches_per_cycle` to 10 in `config/settings.yaml` → cycle drops to ~50s, equity bars refresh every 2 cycles instead of 1 (streaming keeps prices fresh). Or bulk Polygon aggregates as the proper fix (~1hr).
- **`vwap.py:201` + `smc_forever.py:347`** still need `action="sell"` → `action="short"` (carry-over).
- **`max_price` ceiling at $500** still blocks META etc. (carry-over).

### Prior cycle (`62c7d17` — auto-deploy debounce + topology) — KEPT FOR REFERENCE

### `62c7d17` — auto-deploy debounce + skip local-only commits
**Bug 1 (debounce):** every commit triggered an immediate `docker compose up -d --force-recreate`. Three commits in 15 minutes (`dd715d7` 04:16 UTC → `21f6257` 04:18 UTC → `2cf51d1` 04:38 UTC) wiped warmup state 3×. Each recreate eats ~1 full strategy cycle (~2 min) of in-flight signals.
**Fix:** new `DEPLOY_DEBOUNCE_SECONDS` env (default 600s). After each successful recreate, `touch ${REPO_DIR}/.last-deploy`; subsequent ticks within the window log `Debounced — last deploy was Xs ago` and exit *without* pulling (pulling-but-not-recreating would leave the container on stale code AND make the next tick see "no changes" forever). A burst of commits collapses into one recreate at the latest tip once the window passes.

**Bug 2 (same-SHA loop):** `if [ "$LOCAL" = "$REMOTE" ]` only catches the in-sync case. On 2026-05-15 ~03:50-04:00 ET and again 16:35-17:20 ET, the script recreated the container every 5 min for ~30 min while HEAD never moved. Root cause: a session committed directly to local main (`45318e4`, earlier `a1c462c`) and never pushed. Local main was a *descendant* of `origin/main`, so they were unequal but `git pull` was a no-op. Every tick re-detected the same "change."
**Fix:** topology check via `git rev-list --count`:
- `BEHIND > 0` → real changes, deploy
- `BEHIND = 0, AHEAD > 0` → local-only work not pushed, log warning, skip (was the loop)
- `BEHIND = 0, AHEAD = 0` → in sync, exit (was already handled)

**Sandbox validation** (`/tmp/auto-deploy-exercise` with mocked `docker`/`systemctl`, since cleaned up):
| Case | Result |
|---|---|
| In sync | `No changes detected` ✅ |
| BEHIND > 0 | Deploy + `.last-deploy` created ✅ |
| 2nd commit 0s later | `Debounced — last deploy was 0s ago` ✅ |
| Window expired | Deploy latest tip ✅ |
| AHEAD > 0 (local-only) | `Local is N commit(s) AHEAD … Skipping deploy` ✅ |

**Live validation (2026-05-16 07:25 UTC):** simulated BEHIND > 0 by `git reset --hard HEAD~1` on the VPS (HEAD `62c7d17`, origin at `2977e59`). The 07:25:01 tick logged `Changes detected! (1 commit(s) behind, 0 ahead)`, pulled, recreated the container (`StartedAt` advanced `04:30:53Z → 07:25:07Z`, healthy), created `/opt/trading-bot/.last-deploy`, advanced HEAD to `2977e59`. The simulation works because we're committing *from* the same VPS the cron runs on — `git push` leaves LOCAL=REMOTE, so a real external push from a laptop/PR-merge is the only normal way to trigger BEHIND; resetting back one commit reproduces the same state safely (we end at the same SHA we started). Three of five paths now confirmed live (in-sync ×2 ticks, BEHIND > 0 ×1). Debounce + AHEAD > 0 remain sandbox-only — they'll fire naturally on the next 2-commits-in-10-min burst, or any local-only commit left unpushed.

### Crypto observation — "no autonomous crypto trade yet" (open follow-up)
The wires from yesterday's PRs (`c0c2e9d`, `dd611635`-ish — `mean_reversion` + `momentum` injecting BTC/ETH/SOL via dynamic symbols, TradersPost CRYPTO webhook routing) are confirmed working: manual `/api/signal` BTC trade at 00:16 ET went end-to-end (TP CRYPTO 200, mirror confirmed). But in the 14 hours since the wires landed, **exactly one organic algo crypto signal fired** (`momentum: buy ETH-USD @ $2225.41 conf=0.65` at 02:05:29 ET). It was queued in the 02:07:18 ET batch of 9 signals — and never reached risk_manager. The 02:10 ET batch was 20 fresh signals (no ETH); the 132.5s slow cycle (`update_data=102.5s`) caused the next iteration to overwrite the ETH batch before it was processed.

- **`mean_reversion` crypto thresholds** (from PR `21f6257`, code defaults — not overridden in `config/`): `entry_zscore_crypto = -1.2`, `rsi_oversold_crypto = 45`, `rsi_overbought_crypto = 55`. Reasonably loose, but BTC/ETH have been chopping. Looser-than-stocks but still needs an actual pullback bar.
- **132s cycle is the real cost driver**, not strategy conservatism. `update_data=102.5s` per cycle = a 2-min window where any signal can be silently aged out by the next batch. Worth profiling: is it the IBKR historical-bars fetch (`PR #155`-style cap may need tightening), or downstream `_update_scalp` (25.6s)?
- **Practical next step:** monitor the *current* run (started 04:30:53 UTC, now protected by debounce) across an Asian/European crypto session. If mean_reversion still hasn't fired in 24h, drop `entry_zscore_crypto` to `-1.0` and `rsi_oversold_crypto` to `40` — that's still tighter than equity defaults.

### Open follow-ups (carry-over + new)
- **Profile the 132s cycle.** Per-strategy `time.perf_counter()` around each `generate_signals` would localize the long pole. `update_data=102.5s` suggests it's a data-fetch issue, not strategy code.
- **Test paths NOT exercised live yet** in the new auto-deploy:
  - Debounce — will fire on the *second* push within 10 min. Sandbox-validated only.
  - AHEAD > 0 — only triggers if a session commits locally without pushing. Sandbox-validated only.
- **`vwap.py:201` + `smc_forever.py:347`** still need `action="sell"` → `action="short"` (from yesterday's session).
- **`max_price` ceiling at $500** still blocks META etc. (carry-over).

### PR #157 — `mean_reversion` SELL signals gated on ownership
- Diagnosed from morning logs: 115 of 210 rejections were "No position to exit" — mean_reversion firing sells against scanner-discovered overbought stocks (SQQQ, SOXS, ZSL, PIII, etc.) that the bot didn't own.
- `bot/strategies/base.py`: `set_held_symbols(symbols)` on `BaseStrategy`. `None` default = legacy behavior.
- `bot/engine.py`: stamps held-set on every strategy before `generate_signals` (main scan loop AND hot-mover fast lane).
- `bot/strategies/mean_reversion.py:228`: SELL branch returns `None` when symbol not in held set.
- **Out of scope (separate PR worth opening):** `vwap.py:201` and `smc_forever.py:347` use `action="sell"` but reason says SHORT — mislabeled. Both 0% allocated → no live impact. One-line fix.

### PR #160 — Manual-signal path overhaul + signal-staleness root cause
**Five fixes in one branch, all live-validated:**
1. **Truthful API status.** `handle_manual_signal` (engine.py:6260+) detects actual fill via position-state delta (held_before vs held_after, differentiated by buy/short/sell/cover/close). Reports `{status: "blocked", reason: "..."}` on downstream gates. **Validated:** META correctly returned `blocked` (price-filter $500 ceiling), was lying as `executed` before.
2. **Manual exempt from no-quote falling-knife.** `engine.py:4163` adds `signal.source == "manual"` to fail-open set. Legit "quote present + change ≤ threshold" block still fires.
3. **Dashboard snapshot price fallback.** `bot/dashboard/app.py:531` calls `broker.get_snapshot_price(symbol)` when streaming cache misses. **Manual trades now work for ANY symbol IBKR can resolve, not just the ~95 streamed.** Validated live: BAC ($49.40, not streamed) filled clean.
4. **Held positions stream first.** `engine.py:232` builds initial subscription as `held + watchlist`, so the 95-line cap never trims a position out. Boot log now shows `"IBKR real-time streaming initialized (N held + M watchlist)"`.
5. **Signal-staleness root cause + fix.** Morning logs showed 87 rejections with *exactly* "Stale signal: 103s old (max 60s)" — all from one 10:17:33 timestamp, all rejected at 10:19:15. Root cause: `_run_strategies` runs 8 strategies sequentially; each stamps its signals with `datetime.now()` the instant it returns. Strategies 3-8 (rvol_*, momentum_runner, prebreakout, premarket_gap, daily_trend_rider) take ~100s combined to complete, making early-loop signals look 102s stale to the 60s gate. **Fix at engine.py ~3497**: re-stamp `timestamp` + `market_price` for the WHOLE batch with a single `batch_now` right before returning from `_run_strategies`. Now staleness reflects actual pipeline latency (ms), not strategy-loop duration.

### End-to-end execution validated (2026-05-15)
- **Entries:** 5/5 manual `/api/signal` POSTs filled. Path: dashboard → risk_manager → engine → IBKR market order → fill confirmation → `self.positions[sym]` registered → TradersPost mirror (`TP MIRROR: BUY X qty=1 @ $Y → primary webhook (200)`).
- **Exits:** 5/5 manual SELLs filled. Path: dashboard → "Webhook exit signal: routing SELL X through close path" → IBKR market sell → fill → `self.positions.pop(sym)` → TradersPost mirror (`TP MIRROR: EXIT X qty=1.0 → primary webhook (200)`).
- Session P&L: –$11.36 across 5 trades (pure spread cost, no strategy hold).

### Open follow-ups (not yet shipped)
- **Strategy-loop duration itself (~100s/cycle).** The staleness fix in #160 prevents false rejections, but the bot still only does a full strategy scan every ~2 min. Hot-mover fast lane (PR #155) covers momentum names every 3s; the slow-scan delay only affects non-momentum strategies. Worth profiling which of rvol_*, momentum_runner, daily_trend_rider, prebreakout is the long pole. Diagnostic: add per-strategy `time.perf_counter()` around each `generate_signals` call.
- **`max_price` ceiling at $500** blocked META today. Worth checking `config/settings.yaml` `risk.max_price` — if you want to trade higher-priced names (SPY, GOOGL, AVGO, etc. when they go > $500), raise it. Currently META @ $618 was blocked even though it's clearly tradeable.
- **Cosmetic log message.** `engine.py:4177` "FALLING KNIFE SKIP: {symbol} no quote in extended/momentum context (scanner already proved direction)" — the "scanner already proved direction" text is misleading for manual signals (which go through the same fail-open path now). Reword to cover manual case.
- **`vwap.py:201` + `smc_forever.py:347`** — `action="sell"` should be `action="short"` (reason field literally says SHORT). Both 0% allocated so no live impact, but a strategy-allocation change would break it. ~1-line fix per file.
- **Falling-knife "no quote" branch logging.** When manual signals hit fail-open via the no-quote path, log INFO not WARNING (it's expected behavior now, not a precaution).

### What the next session should expect to see
Run morning rejection breakdown:
```bash
awk '/REJECTED:/ { if(/No position to exit/)g++; else if(/Stale signal/)s++; else if(/Price.*away from market/)c++; else o++ } END { print "ghost:",g,"stale:",s,"chase:",c,"other:",o }' logs/trading.log
```
**Expected after #160 deployed:** `ghost: 0, stale: 0` (or near-zero — any remaining staleness is real pipeline latency, not loop duration). Chase rejections may still appear — they're working as designed.

If ghost ≠ 0: a strategy other than mean_reversion is firing unguarded sells (check `vwap.py` / `smc_forever.py` allocations).
If stale ≠ 0: there's a separate pipeline-latency source the #160 batch re-stamp didn't catch. Look at where `filter_signals` is called from sites other than the main loop.

### Auth note for the VPS
- `~/.ssh/github_deploy` is read-only (the existing key, kept for read).
- `~/.ssh/github_deploy_write` is a write-capable deploy key added 2026-05-15 (label `claude-vps-write` in repo Settings → Deploy keys). SSH alias `github-write` in `~/.ssh/config`. Origin URL on the VPS now `git@github-write:femibol/tpstrategyv3.git`.
- Revoke if Claude shouldn't have push access here long-term.

### Auto-deploy thrash gotcha (worth a follow-up PR)
- `*/5 * * * * /opt/trading-bot/deploy/auto-deploy.sh` runs `git fetch origin main; if HEAD != origin/main: git pull origin main; docker compose up -d --force-recreate trading-bot`.
- It does **NOT** switch the working tree's branch first. If a session leaves the tree on a non-main branch (Claude or terminal user), `git pull origin main` either no-ops or creates a wrong-branch merge → infinite restart loop every 5 min.
- Today's session hit this between 17:15–17:25 UTC (3-4 useless restarts). Fix: prepend `git checkout main --quiet` before `git fetch` in `deploy/auto-deploy.sh`. One line.
- Auto-deploy also doesn't pass `--build`, so code-only changes don't actually reach the running image until a Dockerfile/requirements.txt change forces a rebuild. The bot stayed on stale code for hours today despite "successful" deploys. Either always-build or detect significant source changes.

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
