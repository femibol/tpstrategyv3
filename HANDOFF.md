# Session Handoff

Current state of in-progress work so the next Claude Code session picks up without re-deriving context. Update this file **before** the session maxes out.

---

## Last Updated
2026-04-27 — Anthropic SDK migration + Claude Code hooks/commands

## Current In-Progress — claude/pypi-claude-search-q6Qec
Improving the bot's Claude integration and the surrounding Claude Code session
ergonomics. Two pieces, both project-scoped under `.claude/` so they travel
with the repo and work in local terminal + claude.ai/code web sessions:

1. **Anthropic SDK migration** — `bot/learning/ai_insights.py` rewritten to use
   the official `anthropic` Python SDK (was raw `requests`). Wins: built-in
   retries on 429/5xx (was zero retry logic), typed `APIStatusError`/
   `APIConnectionError` handling, and `cache_control` on the static system
   prompt. Caching is a no-op at the current ~500-token system prompt size
   (Opus 4.6 needs ≥4096 tokens to actually cache) but the structure is in
   place if the prompt ever grows. Model unchanged: `claude-opus-4-6`.
   Dependency added to `requirements.txt`: `anthropic>=0.97.0`.

2. **`.claude/` toolkit:**
   - `.claude/hooks/session-start.sh` — installs `requirements.txt` when
     `CLAUDE_CODE_REMOTE=true`, no-op locally. So claude.ai/code web
     sessions can import `anthropic`/`pandas`/etc. without manual setup.
   - `.claude/commands/handoff.md` — `/handoff` slash command. Surveys
     `git log main..HEAD`, drafts the HANDOFF.md update following the
     existing structure, shows diff, commits + pushes on confirmation.
   - `.claude/hooks/handoff-stale-check.sh` — Stop hook. If branch is
     ≥2 commits ahead of main without touching HANDOFF.md, prints a
     one-line stderr nudge at session end. Never blocks.
   - `.claude/settings.json` wires both hooks.

**Deploy on VPS after merge:** nothing. This is dev-tooling + an internal
SDK swap with identical request/response shape; no behavior change for
the running bot. The `anthropic` package will install on next
`pip install -r requirements.txt` (Render auto, VPS via container rebuild).

**Test on VPS or locally:** trigger an AI insights call (e.g. via the
dashboard or by calling `AIInsights.analyze_trades`) and check `logs/`
for `Claude AI Insights enabled (API key configured)`. Errors now log
as `AI Insights API error: ...` (typed) instead of generic exceptions.

## Recently Shipped (merged to main)
- **PR #119** (`claude/alert-backoff-and-recovery-trace`) — Stop alert torture +
  surface why auto-recovery isn't firing. Damps repeated `[ERROR]` Discord
  pings into exponential backoff and adds tracing for the auto-recover path
  so it's clear when the cap-halt or cooldown is the reason.
- **PR #118** (`claude/fix-algo-bot-jhizr`) — Auto-recover wedged ib-gateway
  via Docker socket + `@everyone` pager. `engine.py:_try_auto_recover_gateway()`
  restarts the `ib-gateway` container after 10 failed reconnects (~5 min),
  capped 3/day with 10-min cooldown. Discord `system_alert(level="error")`
  prepends `@everyone` so the phone actually buzzes. Requires `docker` SDK
  + `/var/run/docker.sock` mount (already in `docker-compose.yml`).
- **PR #117** (`claude/fix-algo-bot-jhizr`) — Fix 22h silent IBKR outage. Four
  changes: real liveness check (`ibkr.py:is_connected()` runs
  `reqCurrentTimeAsync()` with 2s timeout, cached 10s — was trusting
  `ib_insync.isConnected()` which only checks TCP), signal-suppression gate
  (`engine.py:_run_strategies()` returns `[]` when broker not live, kills
  phantom signals on Yahoo fallback data), loud escalation (CRITICAL log +
  Discord alert at attempt 10, then every 20 attempts), `bars_warm=0/0`
  display fix (`engine.py:1252-1266` was reading nonexistent `.symbols`,
  now reads `_bars_cache.keys()`).
- **PR #106** (approx) — `ceed18f` enable `mean_reversion` for sideways regime
  resilience (`mean_reversion: 15%`, `momentum_runner: 35%`, was 0%/50%).
  Regime detector's built-in multipliers (×1.4 in SIDEWAYS, ×0.6 in BULLISH
  for mean_reversion) now have a base to scale.
- **PR #105** — Scanner price ceiling filter: dynamic IBKR scanner hits above
  `scanner_max_price` ($500) dropped at injection time. No more phantom
  META/NVDA buy signals.
- **PR #104** — Cycle heartbeat INFO log, IBKR-primary honest log, bind-mount
  `data/`+`logs/` to host, phantom `Score 0 < min 40` fix (risk manager
  stamps `_rejection_reason`; Discord shows real reason; momentum emits
  `score`+`rvol`).
- **PR #103** — Dashboard: removed redundant bottom bar.
- **PR #102** — Dashboard overhaul.
- **PR #101** — IBKR is source of truth for capital.

## Known Ruled-Out Migration Paths (for future sessions)
- **CPGW + ibeam + ibind** — 5 concurrent streaming symbols/session cap (bot
  streams dozens), scanner endpoint returns symbol/name/conid only (bot uses
  rich scanner data), 60 new 1m-bar subscriptions per 10 min (bot rotates
  faster). ibeam itself: stale bundled CPGW JAR, Chrome page crashes
  requiring full container rm+recreate.
- **IBKR OAuth for retail** — not available, no ETA.
- **TradersPost + IBKR** — uses the same CPGW under the hood. Pushes the pain
  onto their infra but loses control.
- **Questrade** — viable but major rewrite (new broker module + scanner from
  scratch). Tabled as warm-standby only.
- **TradingView Pine-script + TradersPost** — would lose Python strategies,
  AI insights, auto-tuner, learning system. Rejected.

## Current Live State (VPS @ 50.116.54.226)
- **Git**: User switched VPS to `main` for the 2026-04-20 deploy (confirmed Done).
  Verify with `git branch --show-current` at session start.
- **Docker**: trading-bot + ib-gateway compose services; bind-mounts for
  `data/` and `logs/` so host tails work.
- **IBKR**: paper account, no 2FA. Gateway has wedged repeatedly with stuck
  post-login dialogs. PR #118's auto-recover handles most of these; rare
  cases still need VNC-in (reach via `<vps_ip>:5900`, NOT `127.0.0.1:5900`).
- **Strategies loaded** (post 2026-04-20 deploy): 8 — momentum 15%,
  momentum_runner 35%, rvol_momentum 10%, rvol_scalp 5%, prebreakout 5%,
  premarket_gap 5%, daily_trend_rider 15%, mean_reversion 15%.

## Still Pending / Gotchas
- **VPS default branch confusion.** VPS sometimes sits on a `claude/*` branch
  rather than `main` — then `git pull` says "Already up to date" even when
  main has new commits. Always verify with `git branch --show-current` +
  `git log --oneline -3` before assuming code deployed.
- **Bar warmup after restart.** Momentum needs 40× 5m bars (~3.3h). Every
  `--force-recreate` wipes the in-memory bar buffer. First trade after
  restart typically not before noon ET.
- **VNC port 5900** is exposed publicly (`0.0.0.0:5900` in docker-compose).
  Works but risky. Offer to bind-localhost-only in a future session.
- **Strategy-level rejections at DEBUG.** `momentum.py:49` and similar log
  skip reasons at DEBUG. If strategies are silent but cycle heartbeat shows
  0 signals, we can't yet see *why* at INFO.
- **AI Insights model** — `bot/learning/ai_insights.py:54` pins
  `claude-opus-4-6`. Migrating to Opus 4.7 requires switching to
  `thinking={"type": "adaptive"}` (4.7 removes `budget_tokens` and sampling
  params); not done in this branch to keep behavior identical.

## Next Up (if user wants more)
- Promote `cache_control` to a real win: either move trade-history JSON
  into the system block (cacheable across same-window calls) or expand
  the system prompt past Opus 4.6's 4096-token cache threshold (e.g.
  with few-shot example trades).
- Bump strategy-level skip reasons from DEBUG → INFO (or add gauge counts
  to the heartbeat line).
- Bind VNC (5900) to localhost only for security; SSH-tunnel required for
  future use.
- Build the `/review-trades` and `/signal-rejections` slash commands
  proposed in the awesome-claude-code review (would replace the manual
  CLAUDE.md "Review Checklist" with one-keypress flows).
- PR #41 stale — verify or close.

## Trade Data Locations (from CLAUDE.md)
- `data/trade_history.json` — every closed trade (bind-mounted to host)
- `data/signal_log.json` — every TradersPost webhook signal (N/A for this
  user, IBKR-only)
- `logs/trading.log` — main bot log (bind-mounted)
- `logs/trades.log` — trade-only log

## How to Use This File
- **Start of session**: read this first, then `git log --oneline -10` +
  `git branch --show-current` (on VPS if deploying).
- **End of session**: run `/handoff` to refresh, or update by hand. Move
  merged items to "Recently Shipped", record open work, push to the
  working branch. The Stop hook will nudge you at session end if the
  branch is ahead and HANDOFF.md hasn't been touched.
