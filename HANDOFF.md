# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-10 — Handoff brief: 7-PR plan (4 low-risk auto-merge + 3 review-required)

## Current In-Progress — `claude/create-handoff-brief-pNbXQ`
Brief-only commit. No code changes on this branch — only this file. The next session
executes PRs 1–4 (auto-merge) here, stops, then PRs 5–7 in subsequent sessions one at
a time with manual verification gates.

---

## How to drive the next session

Tell Claude Code (in a fresh clone of `tpstrategyv3`):

> Read HANDOFF.md and execute PRs 1 through 4. Stop after PR 4 and update
> HANDOFF.md. Do not start PRs 5–7.

PRs 5–7 must each be opened in their own session — they touch auth, tests, and the
8.6k-line engine and need eyes on the diff before merge.

### Open questions (answer before PR 1 starts)
1. **Deps pinning (PR 2)** — pin to the *exact versions currently installed in the
   Render/VPS production image*, or to *latest stable on PyPI*? Production-pin is
   safer (no drift); latest-stable picks up security fixes. **Recommendation: pin to
   currently-installed**, then a follow-up PR can bump deliberately.
2. **License (PR 1)** — README will need a license line. MIT, Apache-2.0, or
   "All rights reserved / private"?
3. **Dashboard auth (PR 5 scope)** — is `bot/dashboard/app.py` already behind a
   Cloudflare Access / nginx basic-auth / Tailscale gate in production? If yes, PR 5
   is a no-op (document the proxy boundary). If no, PR 5 adds Flask-level auth.

---

## The 7-PR Plan

### PRs 1–4 — Low-risk, auto-merge enabled

**PR 1 — Flesh out `README.md`.** Currently 1 line (`# AlgoBot`). Pull architecture
summary from `CLAUDE.md` (engine, brokers, config, learning), point to `SETUP.md` for
install, link `HANDOFF.md` for session state, add license line per question 2 above.
No code touched. Auto-merge safe.

**PR 2 — Pin `requirements.txt`.** Today every line is `>=X.Y.Z`, so `pip install`
on a fresh build can pull a newer minor that breaks ib_async or nest_asyncio
re-entry behavior. Replace `>=` with `==` per question 1. Keep the existing
explanatory comments (ib_async, nest_asyncio, ta-lib, docker SDK rationale — those
are load-bearing). Verify by rebuilding the trading-bot container locally.

**PR 3 — Bind VNC (port 5900) to localhost in `docker-compose.yml`.** HANDOFF
flagged this for weeks: `0.0.0.0:5900` exposes the IBKR gateway VNC publicly. Change
the published port to `127.0.0.1:5900:5900`. Document SSH-tunnel access in SETUP.md
(`ssh -L 5900:localhost:5900 vps`). One-line compose change; tunnel doc is the only
behavior change for the operator.

**PR 4 — Lift strategy-level skip reasons from DEBUG → INFO.** "Next Up" item from
the prior handoff: when `momentum.py` and friends skip a candidate, reason is at
DEBUG so cycle heartbeats show 0 signals with no INFO-level explanation. Bump the
`logger.debug("...skip...")` calls in `bot/strategies/*.py` to INFO, or add a
`skip_reasons` Counter to the cycle heartbeat. Prefer the Counter — keeps log volume
sane on no-trade days. No behavior change beyond log verbosity.

### PRs 5–7 — Need review before merge

**PR 5 — Dashboard auth.** `bot/dashboard/app.py` (Flask) currently relies on
network-level gating (see question 3). If there is no proxy gate, add Flask session
auth: env-var-driven single user (`DASHBOARD_USER` / `DASHBOARD_PASS_HASH`),
`werkzeug.security` hash check, `@login_required` decorator on every route.
**Verify before merging:** open the dashboard in a private window with no cookie —
should redirect to `/login`. Confirm the webhook routes (`/webhook/*` if any) are
exempt or use a separate token, or you'll break TradingView/TradersPost calls.

**PR 6 — Test scaffolding.** No `tests/` directory today. Add `pytest` + a minimal
suite:
- `tests/test_signal_log.py` — round-trip a fake signal through `SignalLogger`,
  assert it lands in `data/signal_log.json`.
- `tests/test_traderspost.py` — mock `requests.post`, assert payload shape and
  rate-limit gate (3s global, 3/60s per symbol, exits bypass).
- `tests/test_trade_analyzer.py` — feed a synthetic trade list, assert win-rate
  math.
Add a GitHub Actions workflow `.github/workflows/test.yml` running `pytest` on push.
**Verify before merging:** CI must go green. Also run locally — these are the first
tests in the repo; expect import surprises.

**PR 7 — Split `bot/engine.py` (8 632 lines).** Highest-risk PR. Suggested seams:
- `bot/engine/loop.py` — main trading loop + cycle heartbeat
- `bot/engine/positions.py` — position management, exits, stop adjustments
- `bot/engine/eod.py` — end-of-day routine
- `bot/engine/recovery.py` — `_try_auto_recover_gateway` + reconnect escalation
- `bot/engine/__init__.py` — re-export `Engine` so existing `from bot.engine import
  Engine` keeps working
**Verify before merging:** (a) `python -c "from bot.engine import Engine"` still
works, (b) start the bot locally for one full cycle, (c) trigger a forced
`docker compose restart ib-gateway` and confirm auto-recovery still fires (this is
the riskiest path to break in a refactor). Do NOT merge if you only smoke-tested
imports.

---

## Recently Shipped (merged to main)
- **PR #129** — TradersPost as execution fallback when IBKR is wedged + restored
  `nest_asyncio.apply()`.
- **PR #128** — Stripped a stray `nest_asyncio.apply()` (turned out to be the
  contextvars bug source under one path).
- **PR #126** — Migrated `ib_insync` → `ib_async` (maintained fork). Drop-in import
  rename. Fixes the Python 3.10/3.11 contextvars re-entry bug.
- **PR #127** — Pinned `gnzsnz/ib-gateway` to `10.37.1r` after `:stable` broke
  overnight.
- **PR #125** — Downgraded base image to `python:3.10-slim` (contextvars spam fix).
- **PR #124** — Pinned `nest_asyncio>=1.6.0`.
- **PR #123** — Reverted PR #117's liveness probe (was breaking ib_async's loop).
- **PR #122** — Shared `ib-gateway`'s network namespace from `trading-bot`.
- **PR #121** — Switched `ib-gateway` image to `gnzsnz:stable` (10.37.1r at the
  time).
- **PR #120** — Added `/handoff` slash command + HANDOFF.md staleness Stop hook.
- **AI insights** switched to the official `anthropic` SDK + web-session install
  hook.
- **PR #119** — Stopped alert torture (Discord backoff) + surfaced why
  auto-recovery wasn't firing.
- Earlier (still relevant): gateway auto-recovery via Docker socket
  (`engine.py:_try_auto_recover_gateway`), `@everyone` on critical alerts,
  bind-mount `data/`+`logs/` to host, mean_reversion enabled at 15% in sideways
  regime.

## Current Live State (VPS @ 50.116.54.226)
- **Branch on VPS**: verify with `git branch --show-current` — has historically
  drifted to `claude/*` branches and silently failed `git pull`.
- **Docker**: `trading-bot` + `ib-gateway` services; `data/` + `logs/` bind-mounted
  to host; trading-bot shares `ib-gateway`'s netns.
- **IBKR**: paper account, no 2FA, gnzsnz `10.37.1r`.
- **Brokers**: IBKR primary, TradersPost as execution fallback (PR #129).

## Gotchas (carried forward)
- **VPS branch drift.** Always confirm `git branch --show-current` before assuming
  a deploy landed.
- **Bar warmup after restart.** Momentum needs ~3.3h of 5-min bars. First trade
  after `--force-recreate` typically not before noon ET.
- **VNC publicly exposed** — fixed by PR 3 above; until then, treat 5900 as hostile.
- **IB Gateway stuck-dialog recurrence** — `docker compose restart ib-gateway`
  usually clears in 2 min; else VNC in (after PR 3, via SSH tunnel).
- **`engine.py` is 8 632 lines.** Any edit risks merge conflicts with PR 7 once
  that lands. If PRs 1–6 finish first, rebase PR 7 last.

## Trade Data Locations (from CLAUDE.md)
- `data/trade_history.json` — every closed trade (bind-mounted to host).
- `data/signal_log.json` — every TradersPost webhook signal.
- `logs/trading.log` — main bot log.
- `logs/trades.log` — trade-only log.

## How to Use This File
- **Start of session**: read this first, then `git log --oneline -10` +
  `git branch --show-current`.
- **End of session**: update "Last Updated", move merged items to
  "Recently Shipped", record open work, push to the working branch.
