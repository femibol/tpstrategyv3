# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-10 (2) — PRs 1, 3, 4 from the 7-PR brief merged. PR 2 blocked on `pip freeze` paste. PRs 5–7 pending review windows.

## Status of the 7-PR Brief

| PR | What | Status |
| --- | --- | --- |
| **1** | DNS pin `8.8.8.8` / `1.1.1.1` on `ib-gateway` in `docker-compose.yml` | ✅ merged (#131, `a2201d8`) |
| **2** | Pin `requirements.txt` (`>=` → `==`) | ⏸ **blocked** — needs `pip freeze` from VPS production container |
| **3** | Yahoo / yfinance fallback gated behind broker-disconnect + 60s per-symbol rate limit + yfinance logger capped at ERROR | ✅ merged (#132, `09bcf0e`) |
| **4** | Real README (was 9-byte stub). All rights reserved license | ✅ merged (#133, `a92da1c`) |
| **5** | Dashboard auth hardening (Basic auth, fail-closed on missing `DASHBOARD_SECRET_KEY`, `before_request` hook, CORS scoped, drop URL-query secret fallback in TradingView webhook) | ⏳ awaits its own session — manual verify required |
| **6** | Unit tests for `risk_manager` + `position_sizer` + `requirements-dev.txt` + (optional) GH Actions workflow | ⏳ awaits its own session |
| **7** | Split `bot/engine.py` (8 632 lines) into `bot/engine/` package via mixins | ⏳ awaits its own session — paper verify required, **do not auto-merge** |

## How to unblock PR 2

On the VPS, run:

```bash
docker compose exec trading-bot pip freeze > /tmp/freeze.txt
scp <vps>:/tmp/freeze.txt ./
```

Paste the contents into the next session and tell Claude: "PR 2: replace
every `>=` in `requirements.txt` with `==` matching this freeze". Preserve
the `ib_async` and `nest_asyncio` block comments — they document load-bearing
context (contextvars re-entry bug, why nest_asyncio is still required).

## How to drive PRs 5–7

Each in its own session. Tell Claude Code, in a fresh clone:

> Read `HANDOFF.md`. Execute PR 5 only — dashboard auth hardening per the
> 7-PR brief in the previous session's git history. Stop at the manual
> verify step and wait for me to run the 5 checks.

Repeat for PR 6, then PR 7. **Do not let auto-merge fire on PR 7** — set
`enable_pr_auto_merge` only if explicitly requested and skip it otherwise.

PR 5 manual verify (5 checks, summarized — full list in the brief):
1. Local boot, hit `:5000` → Basic auth dialog appears.
2. Wrong password → 401.
3. Right password → dashboard loads.
4. Unauthenticated `curl /api/positions` → 401.
5. Empty `DASHBOARD_SECRET_KEY` + `--mode live` → process exits with clear error.

PR 7 manual verify (3 checks):
1. `python -m bot.main --backtest --strategy momentum --symbols AAPL --start 2026-04-01 --end 2026-04-30` runs without ImportError.
2. `python -m bot.main --mode paper --no-dashboard` boots and runs 5 minutes.
3. VPS deploy after market close, watch logs 10 min — no AttributeErrors.

## Recently Shipped (merged to main since the last handoff)
- **PR #133 (`a92da1c`)** — Real README. Replaced the 9-byte stub with project overview: architecture pointer, ops commands, strategy list, license (All rights reserved).
- **PR #132 (`09bcf0e`)** — Yahoo / yfinance fallback gating. `MarketDataFeed._yahoo_gate(symbol)` short-circuits when broker is connected; per-symbol 60s rate limit when not. Wired into `_fetch_bars`, `_fetch_bars_1m`, `refresh_prices`, `get_quote`. Suppresses yfinance INFO logs.
- **PR #131 (`a2201d8`)** — Pinned `8.8.8.8` / `1.1.1.1` DNS resolvers on `ib-gateway`. Matches the manual VPS hand-patch.
- **PR #130 (`3dd50ca`)** — The 7-PR handoff brief itself.
- **PR #129** — TradersPost as execution fallback when IBKR is wedged.
- **PR #128** — Stripped a stray `nest_asyncio.apply()`.
- **PR #126** — Migrated `ib_insync` → `ib_async`.
- **PR #127** — Pinned `gnzsnz/ib-gateway` to `10.37.1r`.
- **PR #125** — Downgraded base image to `python:3.10-slim`.
- **PR #124** — Pinned `nest_asyncio>=1.6.0`.

## Current Live State (VPS)
- **Branch**: confirm with `git branch --show-current` — has historically drifted to `claude/*` branches and silently failed `git pull`.
- **Docker**: `trading-bot` + `ib-gateway` services; `data/` + `logs/` bind-mounted; trading-bot shares `ib-gateway`'s netns. Now also pins DNS on the gateway service (PR #131).
- **IBKR**: paper account, no 2FA, gnzsnz `10.37.1r`.
- **Brokers**: IBKR primary, TradersPost as execution fallback (PR #129).

## Gotchas (carried forward)
- **VPS branch drift.** Always confirm `git branch --show-current` before assuming a deploy landed.
- **Bar warmup after restart.** Momentum needs ~3.3h of 5-min bars. First trade after `--force-recreate` typically not before noon ET.
- **VNC publicly exposed** — still pending fix (it's PR 3 in some prior brief versions; the current 7-PR brief does not include it). Treat 5900 as hostile until bound to localhost.
- **IB Gateway stuck-dialog recurrence** — `docker compose restart ib-gateway` usually clears in 2 min.
- **`engine.py` is 8 632 lines.** Any edit risks merge conflicts with PR 7 once that lands. If touching engine.py before PR 7, plan to rebase PR 7 on top.
- **PR 7 risk surface.** The `_execute_signal` defense-in-depth gate stack (rotation, long-only, crypto block, falling knife, news block, duplicate guard, broker sync, cooldown, stale-signal age, stale-price drift) is load-bearing. Comments document specific historical incidents (WAL, RGNX, NFLX-on-Yahoo). Refactor must preserve every guard.

## Trade Data Locations (from CLAUDE.md)
- `data/trade_history.json` — every closed trade (bind-mounted to host).
- `data/signal_log.json` — every TradersPost webhook signal.
- `logs/trading.log` — main bot log.
- `logs/trades.log` — trade-only log.

## How to Use This File
- **Start of session**: read this first, then `git log --oneline -10` + `git branch --show-current`.
- **End of session**: update "Last Updated", move merged items to "Recently Shipped", record open work, push to the working branch.
