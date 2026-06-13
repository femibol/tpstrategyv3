# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-06-13 (session — Wave 1 deep-dive audit) — **PRs #209/#210/#211 shipped + merged + deployed-ready. Wave 1 strategy purge from the 6/13 audit is COMPLETE.** Catalyst: user explicitly handed me the architect role ("you are the best agent fable 5 and I am on the pro max plan") and said "Ship it" three times — autonomous execution of the audit's immediate plan with VPS smoke test deferred to Sunday evening. **What shipped, in merge order:** (1) **#209 momentum: Donchian breakout primary gate.** The strategy was documented as Turtle Traders (40-50% WR) but implemented as naive EMA crossover (20-28% WR, audit cite); realized 30d WR was 26%, -$500. Fix in `bot/strategies/momentum.py:_analyze_symbol`: `primary_signal` now requires `breakout AND vol_ratio≥1.5 AND ema_bullish AND strong_trend` (ADX > 35 from config). EMA cross demoted from trigger to a +0.10 confidence bonus (was +0.15). New `pullback_to_support` secondary path uses prior-bar low instead of the old 0.5%-above-fast-EMA gate (the old gate killed real pullbacks — they dip BELOW the EMA). Hardcoded 20-mega-cap watchlist (AAPL/MSFT/NVDA/...) removed from `config/strategies.yaml`; strategy operates purely on scanner-injected dynamic universe now. 5 new tests in `tests/test_momentum_donchian_gate.py` pin every gate. (2) **#210 momentum_runner: news catalyst feed wired.** THE big unblock. momentum_runner is the PRIMARY equity strategy at 30% allocation but has been silent for 30 days — root cause: engine never called `feed_catalyst_data()` so the 0-3 Catalyst component of the 10-pt score was permanently 0. Combined with `_analyze_symbol`'s `>30% daily-change` wall (which requires a catalyst record to consider fresh runners), every meaningful candidate was rejected. The strategy's max practical score before this: `3 RVOL + 0 Float + 0 Catalyst + 3 Technical = 6` — exactly the gate, requiring a perfect setup on two axes. Fix in `bot/signals/news_feed.py:get_catalyst_map(lookback_minutes, min_score)`: projects recent BUY signals from `signals_generated` (the news scanner already produces these with classified `catalyst_score`) into the `{symbol: {type, score, reason, published}}` shape `feed_catalyst_data` consumes. Type classifies into `fda`/`earnings`/`upgrade`/`news` from reason-string keywords. `bot/engine.py` at line ~10379 calls `runner_strat.feed_catalyst_data(self.news_feed.get_catalyst_map(lookback_minutes=240, min_score=2))` each Polygon-discovery cycle, wrapped in try/except so news-feed hiccups can't take down the scanner. 10 new tests in `tests/test_news_catalyst_feed.py`. (3) **#211 mean_reversion: per-symbol edge filter.** Audit of 163 crypto closes (5/15→24, mean_reversion only) found NEAR-USD carried the entire book (+$522 / 22 trades / ~50% WR) while ICP/DOT/BCH/LINK/SUI/AVAX/LTC bled ~-$200. The existing `crypto_trend_filter` blocks symbols whose TAPE is currently bleeding but does nothing for symbols that ALWAYS bleed. The engine-wide `should_avoid_symbol` pools ALL strategies so momentum_runner's occasional win on ICP keeps the symbol above the -8 floor while mean_reversion drowns on it. Fix in `bot/learning/trade_analyzer.py:get_symbol_edge_map(strategy, min_trades)`: returns `{symbol: {trades, wins, win_rate, total_pnl, avg_pnl}}` scoped to a single strategy's history (the audit's core insight). `bot/strategies/mean_reversion.py:feed_symbol_edge` + gate inside `_analyze_symbol` block BUY entries when `trades ≥ min_trades AND WR < block_wr_pct AND avg_pnl < 0` (all three required — high-WR symbol with one outsized loser still flows through). Defaults: strategy-level code default OFF (no day-0 blocking on fresh installs), config default ON. Engine wires the feed at top of `scan_strategies` loop. 15 new tests in `tests/test_mean_reversion_symbol_edge.py`. **Pre-flight fix landed too:** PR #209 originally left dangling `- RKLB` / `- SMCI` list items beneath `symbols: []` in `config/strategies.yaml` — invalid YAML that would have crashed bot startup. Caught + fixed in a follow-up commit on the same branch before merge. **Tests:** 389/389 green (was 364 pre-session). **Pipeline state at handoff:** main on `ef1adebd` (post-#211), three Wave 1 PRs merged inside a 13-minute window. Bot has NOT been redeployed to VPS yet — deferred to Sunday evening per the explicit smoke-test plan in the audit synthesis. **Wave 2 (queued, NOT shipped):** (A) distance-to-mean exit for mean_reversion (the audit's symmetric pair with Wave 1 #3); (B) slippage-aware risk caps; (C) per-strategy circuit breakers; (D) portfolio kill switch; (E) auto-tuner overlay (still LEAD from session 10 — the data/ overlay file pattern). **Carry-over follow-ups from prior sessions (unchanged):** FBYD triple-record bug; `low_float_catalyst` still silent; ICP fast-lane → no-fill gap; HTTPS for dashboard; `data/trade_history.json` git rm --cached cleanup. **Sunday smoke-test checklist when redeploying:** (i) bot starts cleanly (the YAML fix is the obvious failure mode if `config/strategies.yaml` somehow merged wrong); (ii) heartbeat shows momentum verdicts mentioning `Vol=X.Yx` / `breakout>Z.WW` instead of pre-fix EMA-cross-led patterns; (iii) at least one log line `News: fed N catalysts into momentum_runner` per scanner cycle when news_feed is live (requires Polygon key); (iv) once 3+ mean_reversion trades exist on a per-symbol basis, watch for the new `EDGE BLOCK: SYM skipped — N trades, X% WR, $Y/trade` log line. **Bridge cheatsheet (unchanged):** `scripts/claude-vps meta` / `trades --last N` / `positions` / `logs --grep X`. Cloud-session VPS commands push to `claude/cmd` via `mcp__github__create_or_update_file`.

2026-05-30 (session 10) — **#181-#184 shipped + DELL incident + auto-tuner overlay queued as next session's lead.** Tonight's PRs in merge order: #181 was the handoff update from session 9; #182 deep-overnight equity guard (`engine.py:~2024` drops equity BUY signals when `_equity_market_open` is False — closes the 20:00-04:00 ET window that fired RKLB -$82.56 at 03:02 EDT and cascaded into a paused-momentum dead day; verified by 44 `EQUITY MARKET CLOSED: dropped N buy signal(s)` log hits overnight on the first night); #183 `strategy_daily_dd_pause_pct: 0.03 → 0.05` (one bad trade no longer nukes a strategy for the entire next session); #184 **default `use_server_bracket: True` for equity entries** (`engine.py:~6281` + config flag `risk.use_server_bracket_equity_default`, master toggle defaults true). **DELL incident the catalyst for #184:** user logged into IBKR once after hours (totally normal user behavior) → IBKR's one-session-per-username rule kicked the bot's gateway → bot's IBKR worker wedged with `IBKR worker call timed out after 180s` on every call → `_update_broker_stop` retry loop couldn't place the broker-side stop on a fresh DELL momentum entry → `/api/control/close/DELL` returned `{"status":"closed"}` but the call sat in the wedged queue → DELL slept overnight uncovered (same pattern as SHOP -$821 incident). #184 fixes the CATEGORY: equity entries now get IBKR-managed bracket child orders at entry time, so stop+TP live on the broker from the moment of entry — survives bot/gateway death entirely. Pattern was already shipped for `low_float_catalyst` (session 5(8)), just extends to all equity. Crypto stays default false (TradersPost webhook path doesn't support IBKR brackets). **Day-of trades 2026-05-29:** clean validation of #182 — **17 trades, +$109.84 net, profit factor 2.18x**, momentum +$99.69 across 13 trades, DELL +$96.56 alone, NO `SAFETY GATE BLOCK` events all day (vs 268 the prior day). Crypto -ish silent (4 trades, +$10.15) because the 5%/3d trend filter correctly blocked a market-wide -3 to -17% dump; user pushed back ("absurd no crypto trades") but heartbeat showed 36 of 42 names in WAIT with strongest at LTC -0.2%, only XRP slightly positive — the filter doing exactly what it was designed for. **Tonight's broken-bot incident** (post-#184 deploy): host had uncommitted auto-tuner edits to `config/settings.yaml` from `config.save_settings()` (called by `auto_tuner.py` at line 248). `git stash + pull + pop` produced a merge conflict at line 9; bot couldn't parse the YAML on next restart; bot was in a `state: restarting / health: unhealthy` loop until I manually resolved with `git checkout --theirs config/settings.yaml + stash drop + restart`. Auto-tuner's edits got dropped (will re-apply within hours). **The deeper problem this exposed and is the LEAD ITEM for the next session:** the auto-tuner writes directly to versioned config files (`config/settings.yaml` + `config/strategies.yaml` via `_save_strategies_yaml` at `auto_tuner.py:522`), causing this exact merge-conflict cascade every time we `git pull`. **The fix (designed but not shipped — explicit user request to wait for clean-baseline day first):** add an overlay file pattern. `bot/config.py` loads `config/settings.yaml`, then overlays `data/auto-tuner-overrides.yaml` (gitignored) on top via deep-merge. Auto-tuner gets `Config.save_setting_override(path, value)` instead of `save_settings()` — writes ONLY to the gitignored overlay. Same for `strategies.yaml` → `data/strategy-tuner-overrides.yaml`. Add `data/*.yaml` to `.gitignore` (currently only `data/*.csv` + `data/*.json` are covered). Migration: existing in-flight tuner edits in `settings.yaml` stay; new edits write to overlay; clean up baked-in edits via one-shot script later or accept they exist. Tests: deep-merge overlay logic, missing-overlay no-op, write-isolation, in-source assertion against `auto_tuner.py:248` to lock the refactor. **Pipeline state at handoff:** bot on `b211db9` post-#184, restarted 2026-05-30T11:00 UTC after the YAML fix, all containers running, positions count: 0 (DELL synced flat from IBKR). Tests: **197/197** green (excluding pre-existing `test_ghost_position_detection.py`). **Watch tomorrow:** (i) first equity entry post-#184 should log `BRACKET ORDER active: <SYM> | SL: $X | TP: $Y` (engine.py:6307); (ii) if it doesn't, the bracket path didn't engage — check `use_server_bracket` evaluation; (iii) `EQUITY MARKET CLOSED` log line should fire overnight 20:00-04:00 ET; (iv) any IBKR worker timeout (`IBKR worker call timed out after 180s`) — flag for the deeper auto-restart fix (option #2 from incident menu). **Open follow-ups for next session (priority order):** (A) **AUTO-TUNER OVERLAY (lead)** — the design above, ~1-2 hr; (B) auto-restart wedged ib-gateway when N consecutive worker timeouts (option #2 of the incident menu — bot already has Docker socket); (C) truthful close-API response (option #3 — currently lies); (D) FBYD triple-record bug still uninvestigated; (E) `low_float_catalyst` still silent (0 signals two days running); (F) ICP-style fast-lane approval → no-fill gap from session 9; (G) CoinGecko scanner still disabled by default; (H) auto-deploy cron's redundant `docker compose build` step; (I) HTTPS for dashboard; (J) `data/trade_history.json` + `signal_log.json` committed to main back in `5396a68` — git rm --cached cleanup. **Bridge cheatsheet (unchanged):** `scripts/claude-vps meta` / `trades --last N` / `positions` / `logs --grep X`. Cloud-session VPS commands push to `claude/cmd` via `mcp__github__create_or_update_file` — cron picks up in 60-120s, result at `cmd/result.txt`.

2026-05-27 (session 9) — **Nine PRs merged in one session + the host-side local-merge fixes resolved. Bridge installed and live; bot restarted at 12:51 UTC on the full stack.** Shipped (in merge order): #172 crypto trail-arm gate (`CRYPTO_TRAIL_ARM_PCT=0.005` to stop the breakeven-stop wick exits flagged in trade review), #173 CoinGecko top-volume scanner (`bot/data/crypto_scanner.py` + `_get_crypto_universe()` engine helper, **off by default** — flip `crypto.dynamic_universe.enabled: true`), #174 `./bot:/app/bot` bind-mount in `docker-compose.yml` + `PYTHONDONTWRITEBYTECODE=1` (carried-over follow-up from sessions 5(8)/5(9) — code changes now need only `docker restart trading-bot-trading-bot-1`, not a rebuild), #175 `crypto_runner` strategy + `new_entrants()` scanner signal (closes the runner-catch gap exposed by tonight's review — mean_reversion buys dips, crypto_runner catches WLD/DRIFT-style pumps with hard stop / hard TP / time stop / no trail; 4% allocation; strict mode via `require_new_entrant: true`), #176 Claude pre-trade hard gate **gated OFF by default** via `ai_pretrade.enabled: false` (live log showed 267 SKIPs / 0 PROCEEDs in one session because the WR rule was reading dirty historical data — momentum's 17% WR was 6/10 `slippage_reject` artifacts from session-6's already-fixed P&L bug `2ec2325` plus pre-fix trail wicks fixed in `e6fcc34`; the gate's auto-tuner model-ID was already fixed in `c26d0bc` — batch AI paths stay on, only the per-signal hot-path gate is off), #177 **VPS bridge — eliminates copy-paste workflow** (`scripts/install-claude-bridge.sh` installs two crons: every 5 min snapshot to `origin/claude/live-state` with `data/*.json` + log tail + docker state, every 1 min cmd-runner polling `origin/claude/cmd` with 90s timeout + 1MB cap; helper `scripts/claude-vps`), #178 bridge fixes (log path fallbacks with diagnostic, `cd $REPO` for docker compose, CLAUDE.md note that cloud sessions can't sign git commits so push cmds via `mcp__github__create_or_update_file`), #179 `git add -fA` in snapshot script (root `.gitignore` `*.log` was silently dropping `review/log-tail.log`, took diagnostic via cmd-runner bridge to find), #180 crypto unblocks (`crypto_trend_min_pct: 0.10 → 0.05` per HANDOFF session 8's pre-approved fallback + crypto exempt from `$0.50` price floor at `engine.py:5814` + renamed misleading "FALLING KNIFE SKIP" to "FALLING KNIFE PASS" since that branch is fail-open, not block). **Plus host local-merge `0afdd38`** keeping LOCAL stricter `top_gainers` thresholds (100K/300K/150K vol + 10% change, comments referenced upstream "raised from 25K" proving local was later) + ibkr `_invalid_symbols_ttl 2h→24h` + new `_qualified_contracts` cache (init `ibkr.py:133`, clear `:1019`, lookup `:1026`, write `:1044` — fixes the SLOW CYCLE 200s+ regression from 2026-05-26 caused by re-qualifying 42 dead contracts per cycle). **Live diagnosis findings tonight from the bridge:** (a) **equity 0-trade root cause was the Claude gate, not threshold tuning** — 2,465 equity signals → 945 risk-manager APPROVED → 267 Claude SKIPs → 0 entries; the "Momentum at 17% WR" rule Claude was applying was a clean rule on dirty historical data; (b) **crypto 0-trade since 23:16 UTC yesterday was 10% trend filter + price floor combined** — 33-36/42 names blocked at 10% with strongest at ICP +9.9% / GRT +8.7% / TIA +7.7%; after #180 deploy heartbeat shows WAIT[31] / neutral[6] (5 more names cleared); (c) **crypto_runner needs a real pump to fire** — no observable test in the visible window. **Bridge end-to-end verified working**: snapshot push from VPS cron readable via `scripts/claude-vps trades --last N`, cmd-runner round-trip via GitHub API takes 60-120s and the result-back commit lands on `origin/claude/cmd` as `cmd/result.txt`. **Pipeline state at handoff:** bot on `8f2d87a` (post-#180), uptime since 2026-05-27T12:51:13 UTC, all 3 containers healthy (trading-bot, ib-gateway, autoheal). User has pulled main + restarted via bridge cmd. **Open follow-ups for next session (in rough priority):** (A) **FBYD triple-record bug** — the same trade dated 2026-05-19T17:14:51 appears 3× in trade_history with identical -$43.40 P&L; inflates loss counts everywhere (look at `_close_position` retry logic in `bot/engine.py`); (B) **low_float_catalyst silence** — fired 0 signals all day with strategy loaded and allocation 5% ($1,199); either no equity name hit RVOL≥5x + day_change≥15% + float≤75M, or scanner isn't feeding it the candidates (verify in tomorrow's premarket); (C) **ICP signals 06:26-06:29 fired 5 fast-lane approvals but 0 orders** — somewhere between `CRYPTO FAST LANE: approved` and `TradersPost SUBMITTED` the signal dropped silently; the next time it happens, grep for the symbol across those two log lines to find the gap; (D) **first 24h of clean equity data** — momentum's actual WR on post-fix code (no slippage_reject inflation, no Claude gate masking) is the question we still can't answer; tomorrow's data is the first honest measurement; (E) data/trade_history.json + data/signal_log.json got committed to main in `5396a68` during this session's first review-data push — they're now tracked-but-gitignored, harmless but should be removed in a small followup PR via `git rm --cached`; (F) the **CoinGecko scanner is shipped but disabled by default** — user hasn't flipped `crypto.dynamic_universe.enabled: true` yet, so the bot still uses the static 42-name list; flip when ready to test (no restart needed since `config/` is bind-mounted, but the strategy loads at boot so a restart IS needed in practice); (G) auto-deploy cron's `docker compose build --quiet` step (`deploy/auto-deploy.sh:234`) is now harmless redundancy with the bot/ bind-mount — could be dropped for ~3-5s faster deploys; (H) scheduled CoinGecko refresh (daily 00:30 UTC) — currently startup-only refresh, one-line `BackgroundScheduler` add; (I) HTTP→HTTPS for the dashboard at `50.116.54.226:5000` — currently sends `DASHBOARD_SECRET_KEY` in cleartext; Cloudflare Tunnel is the free fix. **Tests: 184/184 green** (excluding pre-existing broken `test_ghost_position_detection.py`). **Bridge cheatsheet for next session:** `scripts/claude-vps meta` (freshness), `trades --last N`, `positions`, `logs --grep PATTERN`. For VPS commands from a cloud session, push to `claude/cmd` via `mcp__github__create_or_update_file` (NOT `claude-vps run` — that fails on commit signing); cron picks up in 60-120s, result at `origin/claude/cmd:cmd/result.txt`.

2026-05-25 (session 8) — **14d trend filter on crypto `mean_reversion` entries (`a42210a`, on `main`, not yet redeployed — auto-deploy `*/5` cron will pick it up).** Trade review of `data/trade_history.json`: 163 closed crypto trades since session 7, net +$436 at 61.7% WR — but the book was carried entirely by NEAR-USD (+$522 / 20 trades) while ICP/DOT/BCH/LINK/SUI/AVAX/LTC/BTC/ETH bled ~-$200. Cross-checked NEAR/ICP math by hand against live Coinbase (`scripts/reconcile_crypto.py` had 0 open positions to reconcile) — every row checks out to `qty × (exit − entry)`; the bleed is the strategy, not the paper feed. Discriminator is **14-day prior trend**, not regime (`RegimeDetector` is SPY-based, treats NEAR and ICP identically). NEAR ran +63% over 14d; ICP +7%. Mean-reversion on crypto is "buy dip and ride to time_exit" — wins in an uptrend's drift, bleeds on flat tapes. Naive 24h-prior drift had the **sign backwards** (winners are deeper dips by definition); 14d window separates cleanly. Backtest at +2%/14d threshold: keeps all 20 NEAR ($522), all 10 ATOM, all 5 APT; skips all of ICP/DOT/BCH/SUI/LINK/AVAX/LTC/BTC/ETH; gives up AAVE (+$50) and ETC (+$36) — both posted wins despite negative 14d trend. Net +$104 on the sample (+$436 → +$540). **Implementation:** 3 config knobs in `config/strategies.yaml:42-49` (`crypto_trend_filter_enabled`/`crypto_trend_lookback_bars`=4032/`crypto_trend_min_pct`=0.02); read in `bot/strategies/mean_reversion.py:46-58`; gate runs in `_analyze_symbol:127-141` (slices the existing 30d 5m cache, no extra IBKR fetch); verdict surfaces `WAIT: weak 14d trend (-0.3%)` and `scan_results["trend_14d"]` for the dashboard. **Safeguards:** BUY-only (SELL-side overbought signal still fires for held positions, so existing ICP/DOT can exit cleanly); equity untouched (`_is_crypto` keyed); falls open if cache < N bars (no permanent block on fresh symbols); config-driven. `tests/test_mean_reversion_sell_gate.py` 3/3 green; in-session 5-case smoke covers flat/uptrend/off/equity/short-history. 0 crypto positions open at handoff — clean deploy window. **Sample is only ~10 days; revisit after a week — drop to +0% if too restrictive, raise to +5% if not enough.** Carry-over open from session 7: observed degraded-bot symptoms (`SLOW CYCLE 164.7s`, fast-lane firing every ~3 min, IBKR subs trimmed 76→1) never reproduced this session but not formally diagnosed — watch for re-occurrence.

**Then — hotfix (`0c16a98`, deployed live).** Post-deploy verification caught `a42210a` silently falling open for **every** crypto symbol. Live heartbeat fired BUY signals on LINK-USD (historically a loser, 14d down ~9%) while no symbol showed the `weak 14d trend` verdict anywhere. Smoking gun at `bot/data/market_data.py:350`: `limit = min(1000, 24 * 12 * self.lookback_days)` — Binance.US klines are capped at 1000 5m bars per request (≈3.5 days), not the 30 days `data.lookback_days` implies. Equity gets the full 30d via the IBKR fetcher; crypto is bottlenecked on Binance.US/Yahoo. With the filter requesting 4032 bars and getting 1000 max, the `len(long_bars) >= lookback_bars` guard never tripped, `trend_pct` stayed None, `trend_ok` defaulted True (fall-open). **Retune:** lookback 4032 → **864** bars (3d), threshold 0.02 → **0.10** (+10%). On the same backtest 3d/+10% is essentially equivalent to 14d/+2% (kept +$437 vs +$540; both filter every losing symbol — DOT/ICP/BCH/LINK/SUI/AVAX/LTC/BTC/ETH all blocked, NEAR's +58% over 3d easily clears). Verdict string also made dynamic so the label tracks any future retune (`days = self.crypto_trend_lookback_bars // 288` → `WAIT: weak 3d trend (-0.3%)`). **Live verification at 23:15:50 ET:** first post-fix heartbeat showed **`WAIT[38]: ... weak 3d trend (...)` across nearly the entire universe**, only 3 in `no_data`, 0 BUYs. Top mild positives all blocked (FET +3.0%, GRT +2.0%, RNDR +1.7% — all under +10%); biggest negatives correctly skipped (IMX -18.6%, JUP -18.3%, SNX -17.8%). **Empirical:** +10%/3d is **strict in the current market** (Sunday-night broad crypto consolidation) — zero crypto entries firing right now. If that feels too conservative after a day, **3d/+5% is the natural step down** (backtest kept 19/159, +$384 P&L — would let mild uptrends like FET/GRT/RNDR through). **Path B follow-up (deferred):** paginate `_fetch_binance_us_klines` to fetch 5 windows × 1000 bars rolling backward via `endTime`, restoring the original 14d window (≈460 req/min vs the current ~92, still under the 1200 unauth limit). The 3d filter covers the immediate gap. **Pre-existing bug surfaced (not fixed):** `AUTO-RECOVERY state load failed: 'TradingEngine' object has no attribute 'tz'` warning on every fresh container boot — harmless (treats as fresh state) but should be cleaned up. **Also pre-existing:** `/api/scanner` 500s with `TypeError: Object of type bool is not JSON serializable` (numpy bool leaking through Flask's JSON encoder); dashboard probably has a stale tile. Bot on `0c16a98`, IBKR connected (balance $23,996.38), 47 equity streams live, crypto routes through TradersPost as before.

2026-05-22 (session 7) — **Dashboard modern-UI restyle shipped & deployed live (`e285fcc`).** User asked to "continue the dashboard build" — but no in-progress dashboard work existed anywhere (searched 67 remote branches, both stashes, reflog, the running container; the `dashboard.html.bak.20260522_013652` backup was byte-identical to the live file — a prior session made the backup and saved zero changes). User clarified the intent: "modern ui". Did a **CSS-only restyle** of `bot/dashboard/templates/dashboard.html` — full rewrite of the `<style>` block, **nothing else touched**: no HTML structure, no JS, no element IDs. All 189 class selectors preserved (diffed against HEAD — 0 dropped, 0 renamed) so every data-binding still works. Changes: new palette (deeper bg + blue/violet radial glow, `#5b8cff`→`#9b7dff` gradient accent), sticky glass header + glass bottom nav (`backdrop-filter`), elevation shadows + gradient top-edge on stat cards, pill tabs with gradient/glow on active, animated ping ring on the live status dot, modern toggles/focus-rings/scrollbars, `prefers-reduced-motion` support. Also fixed a latent bug: `--surface` was referenced by `.runner-card` but never defined in `:root`. Verified: Jinja renders clean, HTML tags balanced, CSS braces balanced 261/261. **Deployed:** committed on `claude/dashboard-modern-ui`, fast-forward merged to `main`, pushed, then `docker compose up -d --build trading-bot` (UI isn't bind-mounted, needs a rebuild). Container healthy in 32s, 0 tracebacks, dashboard confirmed serving the new CSS. **ATOM-USD** (the one open position — crypto, mean_reversion, +$28) survived the restart via persisted-state trust, as designed. **Also shipped this session:** the engine.py `slippage_reject` WIP (uncommitted at session start — an 8-line filter excluding `slippage_reject` trades from `_gate_strategy_drawdown` accounting, so an entry-time slippage event can't pause a strategy for the day on a loss it never took) was committed (`81cee03`), fast-forward merged to `main`, pushed, and deployed via a second `docker compose up -d --build`. Container healthy, 0 tracebacks, fix confirmed in the running image; ATOM-USD survived this restart too. **Secret hygiene (`d8312c8`):** rotated `DASHBOARD_SECRET_KEY` in the VPS `.env` to a fresh `secrets.token_hex(32)` value (the old key was already 64 hex chars — not the weak placeholder the prior handoff warned about, so that note was stale); container recreated with `docker compose up -d trading-bot` (env-only change, **no rebuild needed**), verified new key → HTTP 200 / old key → 401. Then deleted four stale secret-bearing `.env` copies that had accumulated in the repo dir (`.env.backup`, `.env.bak`, `.env.save`, `.env.pre-rotate.20260522_022101`) and hardened `.gitignore` — was a bare `.env` rule, now `.env.*` with `!.env.example`, so a stray secret copy can't be `git add`-ed by accident again. The new `DASHBOARD_SECRET_KEY` value was delivered to the user in-session; it lives only in the VPS `.env` (gitignored, never committed). Dashboard URL: `http://50.116.54.226:5000` (user `admin`). Working tree clean on `main` at handoff time (untracked `data/`, `install.cmd`, and `dashboard.html.bak.20260522_013652` are pre-existing, not this session's). All session-7 work is committed + pushed to `main` (`e285fcc`, `81cee03`, `d8312c8` + handoff docs commits); no open branches, nothing in progress.

2026-05-21 (mid UTC, session 6) — **Equity slippage P&L truth + stale-signal gate + IBKR boot-sync retry + TradersPost mirror-orphan reconcile. Four commits, all deployed & verified live: `2ec2325` (3 fixes), `91806d6` (mirror reconcile), `255dcab` (reconcile guard fix). Container on `255dcab`, healthy, 0 tracebacks.** Catalyst was reviewing 2026-05-21 trades: equity recorded −$125 (5 trades, 0 wins, 3 `slippage_reject`), crypto +$40 (9 trades, fine). **The −$125 was mostly fictional.** `_close_position` computed `pnl` and stored `exit_price` from the stale `current_price` parameter, not the close order's `avg_fill_price` — RGTX bought $25.06, sold $25.05 (real −$0.50) but booked −$61 because the reference quote was a stale $23.84. Real equity loss was ≈−$22. Fix 1 (`2ec2325`): `_close_position` overrides `exit_price` with `order["avg_fill_price"]` on the IBKR path; pnl, trade_history, notifier, mirror all use it. Fix 2: pre-fill **staleness gate** — RGTX/RGTU/RL all sat 16-53s in the execution queue (serial processing + Claude pre-trade calls) while the price ran 0.8-7.1% past the signal; the gate re-fetches the live ask right before placing the order and skips if drift > `max_slippage_pct` (doubled outside RTH), instead of buy-then-dump. Fix 3: IBKR **boot-sync retry** — `_init_broker_and_sync` called `get_positions()` once right after connect; IBKR's `positions()` populates async so it routinely returned empty/partial, logging "Synced 0 LONG positions" while the broker held longs. Now polls up to 6×2s until two identical non-empty reads. **KEY DISCOVERY — two accounts.** `.env` has `TRADERSPOST_WEBHOOK_URL` commented out (IBKR-direct execution, correct) but `TRADERSPOST_MIRROR_WEBHOOK_URL` **active**. The bot executes equity on its IBKR paper account (~$24K, balance synced live) and *also* one-way-mirrors every fill to a TradersPost-connected broker (~$89K — the account the user sees in the broker UI). The user's 4 "open positions" (PLTR/FBYD/RIOT/AMZN) are **mirror-account orphans**, NOT IBKR positions — the boot sync correctly reports the IBKR account flat. The mirror is fire-and-forget with no positions API; when a mirrored exit doesn't actually flatten the connected broker (TradersPost returns HTTP 200 even on a rejected "no position" close) the mirror drifts long. Fix 4 (`91806d6`+`255dcab`): `_reconcile_mirror_orphans()` — walks `signal_log.json`, nets equity buy vs exit qty per symbol, surfaces positive net not in `self.positions`. Runs alert-only at boot (WARNING + Discord); closing is opt-in via new `POST /api/reconcile/mirror/run?close=true` (HTTP Basic auth). **Verified live 12:18:06**: `MIRROR RECONCILE: 3 ORPHAN equity position(s) ... RIOT=51, PLTR=26, AAPL=1`. **Limitation:** signal_log records what was SENT not filled — FBYD/AMZN net to zero in-window (phantom-success exits) so the walk MISSES them; only a real positions API on the connected broker would catch those. **Open follow-ups:** (1) user asked to close the 3 detected orphans — endpoint exists, `?close=true` not yet run pending user confirmation; FBYD/AMZN need manual flatten on the broker UI. (2) `tests/test_ghost_position_detection.py` 6 tests fail with `RecursionError` in the test's own `os.path.join` mock (Python 3.12 / pathlib incompat) — pre-existing harness bug, unrelated; rest of suite 146/146 green. (3) this env needed `flask`/`tzdata`/`anthropic` pip-installed to run pytest. **Gotcha:** committing on a feature branch landed on `main` once via a stash round-trip — double-check `git branch --show-current` before commit. The `*/5` `auto-deploy.sh` cron *switches the working tree from any non-`claude/*|wip/*|hotfix/*` branch to `main`* (stash → checkout main → pop) — it yanked an in-progress `feat/` branch mid-edit. **Do code work on a `claude/*` branch** so the cron leaves it alone.

**Then — aggression work (commits `2658a88`, `e01fee9`, deployed `e01fee9`).** User asked to make the bot "more aggressive but considering losses." Finding: the adaptive sizer already does performance-scaled sizing (half-Kelly + drawdown + confidence + session + vol multipliers) but Kelly ran on the **blended** portfolio history — last-100 was 46% WR / negative edge → Kelly pinned at the **0.25× floor for every strategy**, even `mean_reversion` (61% WR, per-strategy half-Kelly = +9% → 2.0×). The bot's best strategy was sized at the *minimum* because `momentum`'s losers (15% WR) dragged the blend negative. Fix: `_kelly_adjustment` takes a `strategy` arg and computes Kelly on that strategy's own record (≥20 of its own trades, else neutral 1.0×); engine passes `signal["strategy"]`. Risk ceiling lowered 3%→2% (`min(adjusted_risk_pct, 0.02)`). **Ramp:** per-strategy Kelly upper bound is now `risk.kelly_max_mult` in `settings.yaml`, set to **1.0** to start (proven strategy sizes at base 1% = 4× its current 0.25%, not the full 2.0× = 8×). **Ramp completed** (`b5cbb0e`): `kelly_max_mult` raised 1.0 → 2.0 — full aggression, a proven strategy (mean_reversion) sizes up to the 2% ceiling on its own edge. **Mirror orphans closed** (`POST /api/reconcile/mirror/run?close=true`): RIOT/PLTR/AAPL flatten EXIT webhooks sent (HTTP 200); post-deploy boot reconcile reports `MIRROR RECONCILE: clean`. Caveat: TradersPost returns 200 even on a rejected close — verify on the broker UI. FBYD/AMZN were never visible to the signal_log walk (phantom-success exits net them to zero) — flatten those manually on the broker if still open. 146/146 tests green.

**Then — momentum trail fix (`e6fcc34`, deployed).** Diagnosed why `momentum` is a 9-15% WR strategy: pulled all 27 momentum trades (net −$312). Two failure modes — (1) `slippage_reject` −$234 (mostly fictional, inflated by the P&L bug; both root causes fixed earlier today), (2) **`trailing_stop` — 10 trades, 0 wins, all exited within ±1% of entry.** Plain `momentum` armed a noise-width 1.5% trailing stop from tick #1, strangling every breakout before it could run. It's an *exit* problem, not a signal problem. Fix: new `_trail_arm_allowed(pos, pnl_pct)` + `MOMENTUM_TRAIL_ARM_PCT=0.02` — a plain-momentum trail is not armed until P&L clears +2%; below that only the hard stop_loss protects it (mirrors momentum_runner's Phase-1). Applied at all 4 trail-arming sites (`_fast_scalp_monitor`, `_on_5sec_bar`, `_on_tick`, `_monitor_positions`); the exit check against an already-armed trail is never gated. mean_reversion/low_float_catalyst/momentum_runner unaffected. 146/146 green.

**Gotchas this session:** (1) NEAR-USD false alarm — broker UI showed −$680 (−23.63%) on a tracked crypto position; cross-checked Crypto.com (real price $1.838 vs broker's glitched $1.392) → position was actually slightly green. The [[project_traderspost_paper_crypto_feed]] glitch again — **always cross-check crypto prices before acting.** (2) The bot was observed running **degraded** ~15:00 ET: `SLOW CYCLE 164.7s` (update_data=134.5s), crypto fast-lane firing every ~3 min instead of 3s, IBKR market data `trimmed to 1 stream` (`Trimming IBKR subscriptions: requested 76, capacity 1/95`). Not yet diagnosed — **open follow-up.** If a crypto position made a genuine fast move toward its stop, the ~3-min monitoring cadence could be too slow to catch it. (3) One-shot Kelly-review cron is armed for 2026-05-22 17:00 ET (`deploy/kelly-review.sh`).

2026-05-18 (late UTC, session 5 (9)) — **Equity trail bug killed (mirror of crypto fix from session 5 (6)) + safety gates fail closed + ATR trail floor + persist-on-close. Three commits shipped + verified live in the running container: `361dc5b` (equity trail floor + block TZA/TNA/BCH-USD/FIL-USD), `97ada5c` (3 hygiene fixes), `49dfcfd` (dedupe TZA — already had an entry in the lower inverse-Russell block).** Catalyst was reviewing today's 64 closed trades (-$11.19 net): equity -$38 (5 trades, only INTC won via `slippage_reject`), crypto +$27 (59 trades) but BCH alone gave back -$63 across 3 losses. Two patterns emerged. (1) TZA and SCCG both exited via `trailing_stop` at -0.6% / -0.9% after 20 min — identical signature to the crypto trail bug fixed in `18ae5f2` (session 5 (6)). The `_fast_scalp_monitor` path was patched then, but the `_on_5sec_bar` (engine.py:3103-3111) and `_on_tick` (engine.py:3197-3204) ratchet paths were NOT — they ratchet on every new HWM regardless of profit/loss state, so a brief wick above entry sets the trail just below entry and the next dip fires the exit. Also patched the `_monitor_positions` long branch at engine.py:3913-3922 (slow fallback poll) for completeness. All three sites now follow the `max(price * (1 - trail_pct), entry)` + `if price > entry` gate pattern from `_fast_scalp_monitor` (line 2996). (2) BCH-USD + FIL-USD were removed from `crypto.symbols` in session 5 (6) but kept re-entering via `_dynamic_symbols` (IBKR scanner injection — bypasses the static list cleanup). Added TZA, TNA (3x inverse/leveraged small-cap pair), BCH-USD, FIL-USD to `config/settings.yaml` `risk.blocked_symbols` so the gate at `engine.py:5093` rejects them regardless of source. Second commit ships three deeper hygiene fixes surfaced by a follow-on code review. **(A) Safety gates fail-OPEN bug:** `_entry_safety_gates` (engine.py:4503-4531) had a bare `except Exception` that returned `""` (= "no gate hit, allow entry"), meaning a single KeyError / network blip / malformed signal would silently bypass SPY breaker + daily trade cap + strategy drawdown + daily drawdown + crypto funding + correlation cluster — nullifying every defensive feature shipped this past week. Now returns `"safety_gate_error"` and logs WARNING. **(C) ATR-based trail with no floor:** `momentum.py:283` set `trailing_stop_pct = atr / current_price` with no floor. On low-priced names like TZA ($4.99) with thin ATR (~$0.03), this resolves to 0.6% — that was the source of today's TZA exit at "trail: 0.6%". Floored at 1.5% (`max(atr/current_price, 0.015)`). Same pattern fixed in `smc_forever.py:310,356`. **(D) Partial-close not persisted:** `_close_position` mutated `self.positions` inside the lock but never called `_persist_positions()`. A crash before the next 3-sec cycle would resurrect the pre-close quantity on restart and re-close already-filled shares. Now persists immediately after the lock block (both partial + full close paths). **Live verification:** rebuilt + recreated `trading-bot-trading-bot-1` at 18:49 UTC (clean window — 0 open positions). Healthy in 31s, IBKR streaming 76 symbols, 42 crypto injected to mean_reversion+momentum (BCH/FIL correctly absent), zero errors/exceptions/tracebacks in startup. All 5 fixes grep-verified inside the running container: `safety_gate_error` at line 4538, "Mirror of the crypto trail fix" comment at 1 site, `max(atr/current_price, 0.015)` in momentum.py:287 + smc_forever.py:310,356, `persist-on-close failed` at line 6387, TZA/TNA/BCH-USD/FIL-USD all present in blocked_symbols. **Gotchas:** (i) `config/` and `bot/` still NOT bind-mounted in `docker-compose.yml` — confirmed today during rebuild (`COPY . .` bakes the whole repo into the image), so any code or config change requires `docker compose up -d --build trading-bot`, not just restart. Same gotcha as session 5 (8). The dedupe in `49dfcfd` was committed but the running container has the (harmless) duplicate; will pick up on next code-bearing rebuild. (ii) Mean reversion review of today's crypto: 0-5 min hold bucket is 21/21 wins / +$44 (partial targets snap back fast), 30-60 min bucket is 1/10 wins / -$67 (death zone — chops out before time_exit can save it), 60-180 min bucket is 14/25 / +$40 (survives to time_exit). Either tighten `entry_zscore_crypto` (currently -1.0) or compress `max_holding_periods` (currently 15 ≈ 75 min) — both need a backtest, deferred. (iii) Overnight hold review: `max_overnight_positions: 0` means the bullish-evaluation code at engine.py:8483-8543 is **dead code for non-trend-rider equity** (capacity check fires immediately). Active overnight policy is: trend rider holds via own bucket + daily ATR stop; crypto bypassed entirely at engine.py:8452 (24/7 market, correct); everything else closed at EOD. Scaffolding kept for if you ever flip the cap > 0. **Deferred follow-ups for next session:** (E) add `./config:/app/config` + `./bot:/app/bot` bind mounts in `docker-compose.yml` so config edits don't need a rebuild — same recommendation carried over from session 5 (8); (F) SHOP-style guard for `_validate_synced_position` at engine.py:4869 (if current_price ≤ would-be-stop on sync, treat as "no stop applied" and wait for price to rise above stop — designed in session 5 (8), still unbuilt); (G) consider a strategy-level `mean_reversion_blacklist` config key (entry-level block via `risk.blocked_symbols` is in place now; strategy-level would skip signal generation entirely and clean up the heartbeat).

2026-05-18 (late UTC, session 5 (8)) — **New `low_float_catalyst` strategy end-to-end + max_positions 10→15 + 3 crypto orphans cleaned out (~$5.4K exposure). Shipped in three commits: `9c56265` (handoff), `eaa1855` (feature), `385b898` (ops endpoint) — all pushed to origin/main.** Catalyst was reviewing today's equity trades (only 4 closed, 1,479 "Max positions reached (7)" rejects because the $10K tier capped at 7 and crypto held 100% of slots), then noticing GOVX/VRAX/SBFM/WGRX all ran 120-163% intraday on tiny floats while the bot's scanner price floor killed every one. Built a dedicated lane for them. **New files:** `bot/data/finviz_float.py` (Finviz HTML scrape, 24h JSON cache at `data/finviz_float_cache.json`; tested live: AAPL 14670M / GOVX 2.89M / VRAX 19.57M / SBFM 4.9M / WGRX 62.7M); `bot/strategies/low_float_catalyst.py` (entry filters: price $0.50-$10, float ≤75M, RVOL ≥5x, spread ≤3%, day_change ≥15%; hard -8% stop, hard +20% TP, 30-min time stop, no trailing; 09:25-09:35 ET open-chop dead zone); `scripts/close_crypto_orphans.sh` (one-shot direct webhook closer with `--dry-run`). **Modified:** `bot/risk/manager.py:48-54` adds `max_low_float_positions` sub-cap + `bot/risk/manager.py:160-168` checks it BEFORE the crypto/equity sub-caps (strategy-keyed, not symbol-keyed); `bot/engine.py` registers the strategy at line 1245, injects scanner runners at line 1337 + line 9430, schedules `_flush_low_float_before_open` at 09:25 ET cron (line 1408), routes `use_server_bracket: True` signals to LIMIT @ price×1.02 so the broker bracket actually attaches (line 5719-5726 — without this the order would be MARKET and `ibkr.py:426` only attaches brackets for LIMIT/MIDPRICE); `config/settings.yaml` bumps tier max_positions 10→15, adds `max_low_float_positions: 5`, adds strategy to both premarket + postmarket `allowed_strategies`; `config/strategies.yaml` adds `low_float_catalyst:` block with allocation 0.05. **Crypto orphan cleanup**: signal-log walk surfaced ATOM-USD 878.89, ICP-USD 718.69, RNDR-USD 1033.92 with no matching exits (~$5.4K unmanaged on Coinbase). Closed via `scripts/close_crypto_orphans.sh` — three HTTP 200 responses with logIds `81260cb2…` (ATOM), `23cc6594…` (ICP), `e7263f03…` (RNDR). **Gotcha — config is NOT bind-mounted:** `docker-compose.yml` only mounts `data/` and `logs/`. The first restart attempt this session kept `max_positions=7` because the container still had the old image-baked config. Workaround used this session was `docker cp config/settings.yaml trading-bot-trading-bot-1:/app/config/settings.yaml` + restart for each file. Permanent fix would be adding `./config:/app/config` and `./bot:/app/bot` bind mounts in `docker-compose.yml` — not done yet because it changes the deploy story for every other file in those dirs. **Live boot at 14:07:25 ET confirmed:** `Scaling tier active: max_positions=15`, `Loaded strategy: low_float_catalyst | Allocation: 5% ($1,203.54)`, no Tracebacks, IBKR streaming healthy. End-to-end sub-cap tested in-process: 4 momentum + 5 low_float → "Low-float sub-cap reached (5/5)" rejects the 6th low_float entry. **`385b898` adds `POST /api/preopen-flush/run`** — same code path as the 09:25 ET cron, used this session to force-fire the flush as a smoke test (response `{"status":"Pre-open flush triggered"}`, log emitted `LOW-FLOAT PREOPEN FLUSH: no positions to close` at 14:20:10). Mirror of `/api/auto-tuner/run` for ops use. **Open follow-ups:** (1) verify first live `LOW-FLOAT SIGNAL` and bracket fill once scanner surfaces a qualifying name (Monitor armed); (2) the SHOP-style guard sketched mid-session (validate `current_price > would_be_stop` before trusting a synced position's fake 3% stop) was designed but never written — see session prose, equity-side `_validate_synced_position` at `bot/engine.py:4869`; (3) consider adding bind mounts for `config/` + `bot/` to skip the `docker cp` dance next session.

2026-05-18 (mid UTC, session 5 (7)) — **Six new defensive features shipped in one batch + trail-migration saga (3 buggy commits before the right answer).** Pipeline: `6a7f403` gate-hit telemetry, `ff3871e` vol-regime sizing dampener, `ae4606f` correlation cluster cap (max 5 concurrent crypto), `a2bbc54` crypto funding-rate filter (OKX), `856ee14` tiered drawdown circuit breaker (-2/-3.5/-5%), `47cca53` volume floor on mean_reversion path 1. As of HANDOFF write time, **`47cca53` is the live deploy and the other 5 are queued behind the 600s auto-deploy debounce** — next eligible cycle is the 08:45 UTC `*/5` cron. **Trail-migration incident:** while live-verifying the `18ae5f2` trail-floor fix, found 5 positions (FIL/SUI/DOT/BCH/RNDR) entered pre-deploy with stale trails at entry × 0.985 — the new `max(..., entry_price)` ratchet only writes when `current > entry`, so stale below-entry trails never self-healed for positions sitting flat. Shipped `cf938ce` per-tick migration → didn't fire for 4/5 positions because `_fast_scalp_monitor` continues at line 2400-2423 when `market_data.get_price()` returns None (which is the steady state for crypto post-restart). Shipped `f5481cf` startup migration (price-independent). **Both migrations had a second bug**: raising `trailing_stop` to `entry_price` while `current_price < entry_price` instantly triggers the exit gate at the next tick. Fired live at 04:19:41 EDT: FIL closed $0.00 (breakeven), BCH -$3.97, RNDR -$1.03 — **the migration itself caused the exits.** Final fix in `034731f`: set trail to **0**, not entry. With trail=0 the exit gate `if trailing_stop > 0 and current <= trailing_stop` stays inactive; the natural ratchet installs a proper trail once price moves above entry. Net today: realized +$10.19 (LINK +3.41 via time_exit clean win on the trail-floor fix; DOT +6.78 via raised stop_loss) − $5.00 (migration bug) = **+$5.19**. SUI is the only open position at HANDOFF write time, sitting at trail = entry (self-healed via natural ratchet when price went +1.2%).

2026-05-18 (mid UTC, session 5 (6)) — **Biggest crypto leak killed: trailing stop can no longer lock in a loss (`18ae5f2`) + self-improvement loop audit (blocker is API credit, not code).** Trade review of 79 crypto rows: mean_reversion is the only viable strategy (+$35.76, 64% wr), partial targets 1-5 are a 100%-wr profit engine (+$103), and the 5-60 min hold bucket is the sweet spot (66% wr). The single biggest leak was `trailing_stop` exits — 8 trades, -$122.75 net, 25% wr — because `_check_position_exits`'s else branch (`engine.py:2755`) set `new_trail = current_price * (1 - trailing_pct)` on tick #1, putting the trail ~1.5% below entry before profit ever existed. Every losing trailing exit (INJ -$46, BCH -$37/-$22, FIL -$21, BTC -$12, LINK -$4) matched this pattern: exited at entry - trail %, the trail had never ratcheted up. Fix in `18ae5f2`: `new_trail = max(current_price * (1 - trailing_pct), entry_price)` and only stored when `current_price > entry_price`. Also added crypto trail floor of 1.5% to block the BTC/LINK 0.1%-trail path (cumulative `min()` chains in `tighten_trail` / HOLD EXPIRING / RUNNER MODE were squeezing the trail too tight). Dropped BCH/FIL/INJ from `crypto.symbols` (combined -$100). Universe now 42. 87/87 tests pass. Auto-deploy due ~05:00 UTC. **Separately:** audited the self-improvement loop — `_claude_pre_trade()` (every signal), `_claude_post_trade_learning()` (every close), `AIInsights.get_quick_insight()` (every 5 trades), `AutoTuner.run_auto_tune()` (cron 12:30 + 16:30 ET Mon-Fri), `WeeklyReview.run()` (Sat 10am ET) — all wired, all silently no-op because `ANTHROPIC_API_KEY` is at $0 balance (confirmed live via container exec: `Error code: 400: Your credit balance is too low`). User opted to top up the API ($20 covers weeks at haiku/sonnet pricing) rather than wire Max via CLI subprocess — the CLI path was reviewed but ToS gray area + Max weekly caps + CLI latency on the hot path made it worse than just paying API. No code change for the AI loop; it resumes the moment the balance is positive.

2026-05-18 (early UTC, session 5 (5)) — **Trade-review improvements shipped (`41b2776`) + every session-5 fix now verified live.** Trade review of 29 deduped logical crypto trades (+$36.26, 33% win, 3 outsized winners carrying it all) surfaced: (1) `momentum` strategy is 1/9 wins (11%) on crypto — disabled it for crypto in `generate_signals` (still gets crypto in `_dynamic_symbols` for `mean_reversion`'s parallel use); (2) `mean_reversion` SELL threshold tightened to z>=1.5 for crypto (was z>=1.0) — z=1.0 was firing on chop and pinging back near break-even; (3) added 5-min min-hold on mean_reversion's own SELL signal for crypto via new `set_held_symbols(symbols, entry_times=...)` kwarg on BaseStrategy. Engine passes `positions[sym]["entry_time"]` at all 3 call sites. Stop-loss + TP unchanged. Deployed at 04:05:15 UTC after the 04:00 cron debounced; survives-restart fix held a **second** time (6 positions restored: SUI, AVAX, LINK, DOT, SOL, ICP — one more than the 03:50 restart proved). First live test of the new SELL guards fired at 00:08:39 EDT: mean_reversion SELL on ICP at z=3.12, RSI=100 — 6:18 after entry (clears 5-min hold), z=3.12 (clears z>=1.5). Engine routed via webhook_exit, closed at +$1.41. 7 positions still open, net unrealized +$5.87.

2026-05-18 (early UTC, session 5 (4)) — **Two follow-on bugs caught LIVE by Improvement A; both fixed in `6205589`.** While verifying session 5 (3), the auto-deploy thrashed on the back-to-back HANDOFF.md commits (`0587ae9`, `8b5754f`) — full rebuild + container recreate each time. The 03:35 UTC restart dropped 3 live crypto positions (AVAX 157.42, DOT 1175.58, XRP 867.04) from `self.positions` because `_load_persisted_positions`'s "not found at broker" check uses IBKR's `get_positions()` — which never knows about crypto. The orphan reconciliation walk (Improvement A from `9855406`) fired correctly at 23:35:19 EDT with `CRYPTO RECONCILE: 3 ORPHAN crypto position(s) likely open on TradersPost` + Discord risk_alert — proving its value in exactly the scenario it was designed for. **3 orphans closed manually via webhook** (logIds: `8729d741`, `930a6575`, `e5bac8f7`). Then shipped `6205589`: (1) `_load_persisted_positions` now trusts persisted state for crypto symbols (since TradersPost has no positions API to confirm against, and Improvement A catches the inverse case); (2) `auto-deploy.sh` computes `git diff --name-only LAST_DEPLOYED..HEAD` and skips the rebuild + recreate if every changed file matches `*.md` / `README` / `LICENSE` / `CHANGELOG` / `HANDOFF*` / `docs/*` — `.last-deploy` still gets updated so the next code-bearing commit triggers a normal deploy. Dry-run confirmed the two HANDOFF commits that caused today's incident classify as DOC_ONLY=true.

2026-05-18 (early UTC, session 5 (3)) — **All session-5 fixes verified LIVE.** `9855406` deployed at 03:20:06 UTC. Within 5 minutes of the new code being up: (a) boot reconciliation walk fired and logged `CRYPTO RECONCILE: clean — no broker-side orphans in the last 48h` (proves Improvement A + the signal_log hygiene from session 5 (2) is consistent); (b) zero `SAFETY GATE BLOCK` messages post-deploy where the previous 3 hours had one every 3 seconds (proves Improvement B's crypto cap split is the binding constraint); (c) two real crypto trades opened — `AVAX-USD 157.42 @ $9.20` and `DOT-USD 1175.58 @ $1.232` — both with `STOP FLOOR APPLIED` and `R/R STRETCH` at INFO (proves `dbe19bf` #3); (d) earlier in the session LTC and LINK each got blocked from immediate re-entry post-close with `CRYPTO RE-ENTRY COOLDOWN: ... closed Xs ago (cooldown 600s) — skipping new entry` (proves `dbe19bf` #4). Only `dbe19bf` #1 (rotation excludes crypto) hasn't been exercised yet — needs ≥6 positions.

2026-05-18 (early UTC, session 5 (2)) — **Improvements A + B shipped: boot-time crypto orphan reconciliation + separate crypto/equity daily trade caps.** Single commit `9855406`. Catalyst was discovering the SUI/ICP orphans from session 5 (1) were just the tip — a signal_log walk over 48h revealed 13 untracked TradersPost crypto positions totalling ~$58K of ungoverned exposure (BTC 0.148, ETH 3.96, XRP 8134, plus 10 others). **All 13 manually closed via webhook (HTTP 200 each, logIds captured)** before this commit landed. Then shipped: (A) `_reconcile_crypto_orphans` at boot — walks `data/signal_log.json` last 48h, surfaces any non-zero positive net qty per crypto symbol as a WARNING + Discord risk_alert; clamps negative net to zero (signal_log over-records exits because TP returns 200 on rejected "no position" exits); also calls `_persist_positions()` immediately after every `self.positions[symbol] = {...}` so a crash before the next 3s scalp tick can't lose an entry; (B) `_gate_global_daily_trade_cap` now bucketed by asset class with separate caps — equity 25, crypto 50 (24/7 market needs the headroom). Earlier today the equity-tuned 25 cap got hit by 18:00 EDT and blocked all crypto for 10 hours. 87/87 tests still pass.

2026-05-18 (early UTC, session 5) — **Crypto churn fixed: rotation no longer rotates out crypto for equity signals, mean_reversion can't immediately re-buy after any close, min_price/max_price no longer reject crypto, STOP TOO CLOSE noise demoted to INFO for crypto.** Single commit `dbe19bf` bundles all four. Surfaced from reviewing today's 39 crypto trades (~$40 reported P&L) and the user's observation that SUI + ICP were still open on TradersPost while the engine reported 0 positions. Orphans closed manually via webhook before the commit landed (SUI 2725.97628, ICP 1101.25468 + 1123.15719, all HTTP 200 with logIds in the commit message). Also confirmed: the multi-row ETC/BCH/AAVE history entries (6 ETC closes for one entry) are legit partial-profit fills via `_partial_close_inner`, not a bug.

2026-05-17 (late UTC, session 4 (2)) — **Two crypto trades shipped + slow-cycle FALLING KNIFE noise silenced + per-broker rate-limit so crypto bursts all fire.** Two more commits after the initial session-4 push: `c237aa9` (falling-knife fail-open now keys on `is_crypto_symbol(symbol)`, not the `_crypto_fast_lane` flag — fixes slow-cycle WARNING noise that the 7c04107 fix didn't cover), `397c78e` (`GLOBAL_MIN_INTERVAL` is now per-instance via `min_interval_override`; crypto broker set to 0, so 3-5 simultaneous fast-lane approvals all fire instead of N-1 dropping with `NO EXECUTION PATH AVAILABLE`). Second live trade landed at 17:50:04: `TradersPost SUBMITTED: ETH-USD qty=1.32102 @ $2,192` (SL $2148.87 / TP $2280.43). 87/87 tests still pass.

2026-05-17 (late UTC, session 4) — **FIRST AUTONOMOUS CRYPTO TRADE FIRED.** Three commits this session unblocked the entire crypto path end-to-end: `7c04107` (falling-knife bypass), `07f4a3f` (crypto pinning in dynamic-symbols cap — the actual root cause of `no_data=45` heartbeats), `d3e2d75` (separate IBKR mirror from crypto). Live validation at 17:33:01 UTC: `TradersPost SUBMITTED: BTC-USD qty=0.03693 @ $78,439` via the CRYPTO webhook (HTTP 200), full SL/TP set, momentum-strategy entry. Also: 87/87 tests pass after fixing the long-broken `test_ibkr_outside_rth_cancel_policy.py` fixture (`_FakeContract` was missing `(exchange, currency)` positional args, and the test asserted `queued` when the broker actually returns `deferred`).

2026-05-16 (late UTC, session 3) — **Crypto pipeline complete: 45-name universe on Binance.US real-time bars, fast lane firing every 3s, fractional sizing through risk_manager, truthful heartbeat — and all of it validated live as `universe=45 | neutral=45` at 11:50:53 ET.** Four commits since the prior handoff: `75789b9` (universe 3→46 + fast-lane reads config + bucketed heartbeat), `8bb89bf` (Binance.US adapter primary, Yahoo fallback for MKR/TON, MATIC→POL + RNDR→RENDER alias map, STX dropped), `108cb91` (heartbeat WAIT verdict bucketed as no_data, `LOG_LEVEL` env var so future sessions aren't blind to `log.debug` like this one was). Bot is now in the "waiting for a real signal" state for the first time — pipeline works end-to-end, market is just quiet on a Saturday afternoon.

### `6a7f403` — gate-hit telemetry

Six new defensive gates were shipped this sub-session and we had no way
to measure if they actually fired or which symbols triggered them.

In-engine counters: `self._gate_hits` (per-gate per-symbol), `self._gate_hits_total`
(per-gate totals), `self._gate_recent` (last 50 hits with reason + ts). All
six gates in `_entry_safety_gates` call `_record_gate_hit(name, symbol, reason)`
on a block — `spy_circuit_breaker`, `daily_trade_cap`, `strategy_drawdown`,
`daily_drawdown`, `crypto_funding`, `correlation_cluster`.

Exposed via `/api/status` under `gate_hits` with `totals`, `by_symbol`, and
`recent` (last 20 for the dashboard tail). Daily reset lives in
`_pre_market_scan`. The volume floor in `mean_reversion.py` is a strategy
verdict not a gate, so it's NOT instrumented here — add separately if hit
counts on that matter.

### `ff3871e` — vol-regime sizing dampener

The existing sizer at `bot/risk/position_sizer.py:202` already vol-targets
implicitly: `qty = risk_dollars / per_share_risk` where `per_share_risk = 2 × ATR`.
But ATR is computed over 14 bars; when the vol REGIME shifts (flash event,
news, outage), ATR lags by several bars and stop-distance understates
current risk. Dollar risk per trade ends up 1.5-2x intended exactly when
you can least afford it.

`_compute_vol_regime_mult(symbol)` at `engine.py` (right before `_execute_signal`)
compares short-window realized vol (last 10 5-min log returns ≈ 50 min) to
a longer baseline (last 60 ≈ 5 hours). Ratio < 1.5 → 1.0 (neutral). Ratio
1.5-3.0 → linear 1.0 → 0.5. Ratio > 3.0 → 0.4 floor. Multiplier is clamped
`[0.4, 1.0]` in `position_sizer.calculate` — protective only, never sizes UP.

Passed as new `vol_regime_mult` kwarg through the main entry path at
`engine.py:5165`. Secondary call site at line 9765 (partial-close rebuy)
is NOT wired yet — less relevant there, can add if needed.

### `ae4606f` — correlation cluster cap (max 5 concurrent crypto)

Generic `max_positions=7` doesn't prevent a "diversified" book that's
really 1 position on BTC beta — alts trade ~0.7+ correlated with BTC most
regimes, so a 3% BTC drop hits all 7 stops simultaneously.

`_gate_correlation_cluster(symbol)` in `_entry_safety_gates` at engine.py:4316.
First-pass version uses asset-class clustering (all crypto = one bucket);
true pairwise correlation over rolling windows is a follow-up. Equity not
capped yet — only 9 equity rows in history, not enough to define sector
clusters. Config knob at `config/settings.yaml` `crypto.max_concurrent_positions`
(default 5).

### `a2bbc54` — crypto funding-rate filter (OKX)

Mean reversion breaks down when perpetual funding is heavily one-sided —
that's the market saying "this is a real directional move, not noise" via
perp/spot premium. Long mean-revert in heavy negative funding is fighting
both price and carry.

Source: **OKX public API** — Bybit is 403 from this VPS, Binance.com perp
is 451 geo-blocked. Tested 42/42 symbols and 40 have OKX perpetual coverage;
FET-USD and MKR-USD fail open (no block on missing data, trade normally).
Symbol map: `BTC-USD` → `BTC-USDT-SWAP`, reuses the existing POL/RENDER
alias map from `_BINANCE_ALIASES`.

`_get_crypto_funding_rate(symbol)` 5-min per-symbol cache. `_gate_crypto_funding_extreme(symbol)`
blocks when `|funding| > 0.0005` (0.05%/8h = ~55%/yr annualized — already
extreme regime where carry dwarfs mean-reversion targets of ~0.5-1.5%/trade).
Fail-open on any error so a network blip doesn't block trading. Wired
into `_entry_safety_gates` AFTER drawdown gate. Equity entries unaffected
(short-circuit on `not self._is_crypto_symbol(symbol)`).

### `856ee14` — tiered daily drawdown circuit breaker (crypto + equity)

Existing `_check_daily_loss_soft_stop` fires only AFTER a trade closes —
gap means if losses come from unrealized swings recognized later, no pause
triggers. New `_gate_daily_drawdown()` runs on every BUY signal (covers
both crypto and equity, since both go through `_execute_signal` at line
4596 → `_entry_safety_gates` → this gate).

Tiers against realized daily P&L pct vs `start_of_day_balance`:
- **-2.0%** → 1h entry pause (mirrors existing soft-stop)
- **-3.5%** → 4h entry pause
- **-5.0%** → halt for rest of day

State (`_dd_block_until`, `_daily_soft_stop_active`) reset in `_pre_market_scan`.
Hard `stop_loss` + `risk_manager` still handle position-level risk; this is
the portfolio-level brake. Why pros use this: losing streaks cluster in
regime changes — once you're down 3% intraday, the regime hasn't shifted
yet and the next 5 trades have skewed-negative expectancy.

### `47cca53` — volume floor on mean_reversion path 1

Trade review found path 1 of `entry_ready` (`zscore_ok AND rsi_oversold AND
reversal_candle`) had **no volume gate** while paths 2 and 3 required
`vol_ratio > 1.3` and `> 1.5`. Path 1 is the most common entry trigger and
lets through low-volume "chop" signals that ping back to entry.

Added `vol_ratio >= 1.1` to path 1 at `bot/strategies/mean_reversion.py:171-175`.
1.1x is a soft floor — well under paths 2/3 so we don't choke off real
signals, but blocks thin-volume z=-2 chop. Applies equally to crypto and
equity (single strategy, both venues). Verdict adds a "WAIT: needs vol>=1.1x"
branch so the heartbeat tells the truth when volume is the only missing
piece.

### Trail-migration saga (`cf938ce` → `f5481cf` → `034731f`)

Three commits, two bugs, one $5 lesson. Order of events on 2026-05-18:

1. **`cf938ce` per-tick migration** — Added a block at the top of the long
   branch in `_fast_scalp_monitor` (engine.py:2766+) that detected stale
   below-entry trails and raised them to entry. Worked for SUI (the only
   position with a fresh `current_price`) — log line at 04:19:41 EDT:
   `TRAIL MIGRATION: FIL-USD trail $0.9131 → $0.9270 (entry floor)`.
2. **First bug surfaced immediately:** raising trail to entry while
   `current_price < entry_price` instantly triggers the exit gate at the
   same tick. **FIL closed $0.00**, **BCH closed -$3.97**, **RNDR closed
   -$1.03**. The migration itself was the cause. DOT survived because
   `current` ticked above entry between migration and exit-check.
3. **`f5481cf` startup migration** — Moved the migration to
   `_init_broker_and_sync` so it didn't depend on price availability. Same
   `pos["trailing_stop"] = entry_price` logic — would have caused the same
   instant-exits on any future restart of below-entry positions.
4. **`034731f` final fix** — Set trail to **0**, not `entry_price`. The
   new exit gate is `if trailing_stop > 0 and current <= trailing_stop`,
   so trail=0 leaves the gate inactive. Natural ratchet installs a proper
   trail once price moves above entry. Hard `stop_loss` (~4-5% for crypto)
   handles downside in the meantime — which is exactly the behavior a
   fresh post-18ae5f2 entry would have.

**Lesson for future trail logic:** never raise a stop ABOVE current price.
"Migrate stale trail" semantically means "treat as if it never existed,"
not "preserve at entry." When in doubt, unset and let the natural code
rebuild.

Also surfaced: the per-tick migration's `current_price is None` early-
`continue` at engine.py:2400-2423 was why FIL/DOT/BCH/RNDR didn't migrate
on cf938ce alone — their crypto symbols don't have IBKR security definitions,
so `market_data.get_price()` returns None until Binance.US/Yahoo feeders
warm. SUI was the only position with a fresh price the first 4 minutes
post-restart. The startup migration in f5481cf (kept in 034731f) is
price-independent and is the durable answer.

### `18ae5f2` — trailing-stop gate + crypto trail floor + drop BCH/FIL/INJ

Trade review of 79 crypto rows in `data/trade_history.json` (51 full closes + 28 partial-fill rows, net +$21.89 all-in). Strip the noise and the data is loud about three things:

**What's working (keep):**
- `mean_reversion`: 67 trades, +$35.76, **64% wr** — only viable crypto strategy.
- Partial targets 1-5: 28 trades, +$103, **100% wr** by construction — the lock-in mechanism is the profit engine.
- `time_exit`: 16 trades, +$58, 56% wr — letting winners ride to the hold cap pays.
- 5-60 min hold window: 49 trades, +$34, 66% wr — entries that work, work fast.
- AAVE, ETC, NEAR, ICP, XRP, SOL combined +$144.

**What's bleeding (fix):**
- `trailing_stop` exits: 8 trades, **-$122.75 net, 25% wr.** The single biggest leak. Root cause below.
- `rotation` exits: 13 trades, -$38.39, 7.7% wr. **Already fixed** in `dbe19bf` (session 5 #1). Last rotation exit: 2026-05-17 19:00 EDT. Holding.
- `momentum` on crypto: 10 trades, -$13.87. **Already fixed** in `41b2776` (session 5 (5)). Zero momentum crypto entries since.
- INJ -$46, BCH -$32, FIL -$20 (10 trades, all `trailing_stop`).
- 1-4h hold bucket: -$12.73 — symptom of the trailing-stop bug catching positions before `time_exit` fires.

**The trailing-stop bug (root cause).** `bot/engine.py:2755` in the non-momentum-runner else branch (which is every `mean_reversion` crypto position):

```python
new_trail = current_price * (1 - trailing_pct)
if "trailing_stop" not in pos or new_trail > pos.get("trailing_stop", 0):
    pos["trailing_stop"] = new_trail
```

On tick #1 of a fresh entry, `current_price ≈ entry_price`, so `new_trail ≈ entry - trailing_pct`. The trail is stored at ~1.5% BELOW entry before profit ever existed. Any tiny dip then triggers `current_price <= pos["trailing_stop"]` and the position exits for a small loss. The pattern is exact:

```
INJ  entry 4.6750 → trail 1.5% → exit 4.6000 (-1.60%)   -$46.47
BCH  entry 385.80 → trail 1.5% → exit 373.90 (-3.08%)   -$37.23
BCH  entry 379.50 → trail 1.5% → exit 372.60 (-1.82%)   -$21.94
FIL  entry 0.9390 → trail 1.5% → exit 0.9270 (-1.28%)   -$21.29
BTC  entry 78400  → trail 0.1% → exit 78079  (-0.41%)   -$11.87
LINK entry 9.5740 → trail 0.1% → exit 9.5480 (-0.27%)   -$ 3.93
```

The only two winning trailing exits (ETC +$10, ICP +$10) were the ones where price ran far enough to ratchet the trail above entry first. The momentum_runner branch above (`engine.py:2622+`) already handles this correctly — Phase 1 (`pnl_pct < 2%`) sets `trailing_pct = 0` so no trail until profit. The else branch had no such gate.

**Fix.** `engine.py:2767`:

```python
new_trail = max(current_price * (1 - trailing_pct), entry_price)
if current_price > entry_price and (
    "trailing_stop" not in pos or new_trail > pos.get("trailing_stop", 0)
):
    pos["trailing_stop"] = new_trail
```

The `max(..., entry_price)` floor means the trail can never lock in a loss. The `current_price > entry_price` gate means the trail isn't written on every losing tick (which would otherwise cause an exit at breakeven on the next downtick). Exit gate also strengthened: `if pos.get("trailing_stop", 0) > 0 and current_price <= pos["trailing_stop"]` — the > 0 check matters because the post-fix unset state must not trigger a spurious exit.

Applies to all non-runner positions (equity too). The bug isn't crypto-specific; crypto just had the volume to expose it. Equity has its own hard `stop_loss` at entry (engine.py:2562-2570), so downside protection is unchanged.

**Crypto trail floor.** Added at `engine.py:2706`: clamps `trailing_stop_pct` to ≥1.5% for crypto, regardless of what `tighten_trail` / HOLD EXPIRING / RUNNER MODE paths did to it. Blocks the BTC/LINK 0.1%-trail pattern without unwinding the offending paths. Hard-coded for now; the right long-term move is per-asset-class bounds in `auto_tuner.PARAM_BOUNDS`.

**Dropped from `config/settings.yaml` `crypto.symbols`:** BCH-USD, FIL-USD, INJ-USD. Universe 45 → 42. These three combined for -$100 across 10 trades, all `trailing_stop` blowups — ATR is large relative to entry size. Revisit if the gate fix makes them net-profitable on new behavior.

**News-trail branch (engine.py:2706-2724) NOT changed.** It's reactive to news events and the "lock in profit fast even at a small loss" semantic is intentional there. Left alone.

87/87 tests pass. Auto-deploy due ~05:00 UTC.

### Self-improvement loop audit (session 5 (6))

User asked: "always self improve depending on trades. you can use Claude also for decision taking finding the best exit and entry." Investigation confirms the loop is already fully built; the only blocker is API credit.

| Component | When | Source |
|---|---|---|
| `_claude_pre_trade()` | Every signal | `bot/engine.py:6434` — returns `skip` / `reduce_size` / `aggressive` verdicts |
| `_claude_post_trade_learning()` | Every trade close | `bot/engine.py:6669` |
| `AIInsights.get_quick_insight()` | Every 5 trades | `bot/learning/ai_insights.py` |
| `AutoTuner.run_auto_tune()` | 12:30 + 16:30 ET, Mon-Fri | `bot/engine.py:1232`; bounds in `bot/learning/auto_tuner.py:PARAM_BOUNDS` |
| `WeeklyReview.run()` | Sat 10am ET | `bot/learning/weekly_review.py` |

All five hooks call `self.ai_insights.is_available()` first and silently return if not. Live test inside the container confirms `ANTHROPIC_API_KEY` returns 400 "credit balance too low" — so every hook is currently a no-op. Top up at https://console.anthropic.com/settings/billing and the entire loop resumes with zero code change.

**Claude Max via CLI considered + rejected.** User asked if their Max subscription could replace the API. Technically possible (mount `/root/.local/bin/claude` + `~/.claude/.credentials.json` into the container, replace `_call_claude()` with subprocess to `claude -p`), but: (1) Max weekly caps would conflict with Claude Code sessions; (2) ToS gray area for high-volume programmatic use; (3) ~1-3s CLI startup vs ~500ms API on the pre-trade hot path. User opted for API top-up. Don't re-litigate unless asked.

**Real gaps to keep in mind (not fixed this session):**
- Auto-tuner only runs Mon-Fri weekdays — crypto is 24/7 and never gets a crypto-specific tune cycle.
- `PARAM_BOUNDS` in `auto_tuner.py` are global, not per-asset-class. The 1.5% crypto trail floor I just hard-coded should eventually live there.
- Pre-trade Claude prompt is generic in `ai_insights.py:SYSTEM_PROMPT`; could feed crypto signals their z-score / RSI / recent same-symbol exits for sharper entry calls.

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
