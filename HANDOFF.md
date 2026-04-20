# Session Handoff

Current state of in-progress work so the next Claude Code session picks up without re-deriving context. Update this file **before** the session maxes out.

---

## Last Updated
2026-04-20 — sideways-regime rebalance + IBKR recovery

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
