# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-10 (3) — End of session. **All 7 brief items resolved**: PRs 1–6 merged, PR 7 deferred.

## Summary

This session executed the 7-PR brief that landed on `main` as PR #130. Six PRs merged cleanly today; PR 7 is deferred to its own session.

| PR | What | Status |
| --- | --- | --- |
| 1 | DNS pin (`8.8.8.8`/`1.1.1.1`) on `ib-gateway` | ✅ #131 (`a2201d8`) |
| 2 | `requirements.txt` pinned `>=` → `==` from production `pip freeze` | ✅ #137 (`0e48c6f`) |
| 3 | Yahoo / yfinance fallback gating + 60s rate limit + logger suppression | ✅ #132 (`09bcf0e`) |
| 4 | Real `README.md` (was 9-byte stub) | ✅ #133 (`a92da1c`) |
| 5 | Dashboard auth hardening + TradingView webhook tighten | ✅ #135 (`7148b55`) — **merged unverified at user request, see "Post-merge actions" below** |
| 6 | `tests/` scaffold + 59 unit tests for `RiskManager` and `PositionSizer` + GH Actions | ✅ #136 (`b42c9d0`) |
| 7 | Split `bot/engine.py` (8 632 lines) via mixins | ⏳ deferred — own session, paper verify required, **do not auto-merge** |

## Post-merge actions for PR #135 (dashboard auth)

Merged without the manual 5-check verify because the prior verify accidentally tested `main` (the working tree never switched off `main` — `git checkout` had aborted on a local `docker-compose.yml` diff that turned out to be the now-redundant DNS hand-patch). So PR #135 has not been live-verified end to end.

**Before/during the next deploy on the VPS:**

1. Confirm `DASHBOARD_SECRET_KEY` is set in `/opt/trading-bot/.env` to a real value (not the `verify-only-123` placeholder). Suggested rotation:
   ```bash
   sed -i 's/^DASHBOARD_SECRET_KEY=.*/DASHBOARD_SECRET_KEY='"$(openssl rand -hex 32)"'/' /opt/trading-bot/.env
   ```
2. After `docker compose build trading-bot && docker compose up -d --force-recreate trading-bot`, watch logs for `RuntimeError: DASHBOARD_SECRET_KEY must be set` — that means the env var is empty and the dashboard refused to start (intentional fail-closed).
3. From the VPS shell, repeat the curl checks (these will now actually exercise PR 5 because main has the merge):
   ```bash
   curl -i -s http://localhost:5000/api/positions | head -3              # expect 401
   curl -i -s -u admin:wrong http://localhost:5000/api/positions | head -3   # expect 401
   curl -i -s -u admin:<your-secret> http://localhost:5000/api/positions | head -5  # expect 200
   curl -i -s http://localhost:5000/health | head -3                     # expect 200 (public)
   ```
4. Browser to the dashboard via SSH tunnel — Basic auth dialog should appear.

## How to drive PR 7 (engine split)

In its own session, in a fresh clone, after market close. Tell Claude:

> Read `HANDOFF.md`. Execute PR 7 only — split `bot/engine.py` (8 632 lines) into a `bot/engine/` package via mixins per the original 7-PR brief in PR #130's history. Stop at the manual verify step and wait for me to run the 3 paper-verify checks. Do not enable auto-merge.

PR 7 manual verify (3 checks):
1. `python -m bot.main --backtest --strategy momentum --symbols AAPL --start 2026-04-01 --end 2026-04-30` runs without ImportError.
2. `python -m bot.main --mode paper --no-dashboard` boots and runs 5 minutes.
3. VPS deploy after market close, watch logs 10 min — no AttributeErrors. Force a `docker compose restart ib-gateway` and confirm auto-recovery still fires (riskiest path to break in a refactor).

## Recently Shipped (merged today)
- **#137 (`0e48c6f`)** — Pinned `requirements.txt` from production `pip freeze`.
- **#136 (`b42c9d0`)** — pytest scaffold + 59 unit tests + GH Actions workflow.
- **#135 (`7148b55`)** — Dashboard Basic auth via `before_request` hook (every route except `/health`), fail-closed on missing `DASHBOARD_SECRET_KEY`, scoped CORS, dropped 17 `@self._require_auth` decorations, removed secret from template render. TradingView webhook switched to `hmac.compare_digest`, dropped URL-query secret fallback, fail-closed in live mode.
- **#134 (`ac7cfb6`)** — Mid-session HANDOFF status update.
- **#133 (`a92da1c`)** — Real README with All Rights Reserved license.
- **#132 (`09bcf0e`)** — `MarketDataFeed._yahoo_gate(symbol)`: hard-skip when broker connected; per-symbol 60s rate limit when not. Wired into `_fetch_bars`, `_fetch_bars_1m`, `refresh_prices`, `get_quote`. Caps yfinance logger at ERROR.
- **#131 (`a2201d8`)** — DNS resolvers pinned on `ib-gateway`.
- **#130 (`3dd50ca`)** — The 7-PR handoff brief itself.

(Earlier merges in `git log --oneline -30` cover the TradersPost fallback, ib_async migration, gnzsnz pin, base-image downgrade, etc.)

## Current Live State (VPS)
- **Branch on VPS**: confirm with `git branch --show-current` — must be on `main` and `git pull` should be clean. Earlier today the VPS was 7 commits behind because a `git checkout` aborted on a local `docker-compose.yml` diff (the DNS hand-patch). User ran `git checkout -- docker-compose.yml && git pull && docker compose build trading-bot && docker compose up -d --force-recreate trading-bot` — bot booted on `0e48c6f` with $24,584.51 paper balance, 2 long positions, 8 strategies, 94 IBKR streams.
- **Brokers**: IBKR primary (`gnzsnz/ib-gateway:10.37.1r`), TradersPost as execution fallback (PR #129).
- **Untracked-on-VPS leftovers**: `.env.backup`, `.env.save`. Harmless but worth `rm`ing.

## Gotchas (carried forward + new)
- **Dashboard now refuses to start with empty `DASHBOARD_SECRET_KEY`.** If a future deploy crashes with `RuntimeError: DASHBOARD_SECRET_KEY must be set`, set the env var. This is intentional fail-closed.
- **Dashboard JS still references an `AUTH_KEY` constant** (see `bot/dashboard/templates/dashboard.html:1107`). With Basic auth handled by the browser session, that path is now a no-op. Optional cleanup: drop the JS constant and the `dashboard_key=""` template arg in `bot/dashboard/app.py`.
- **IBKR disconnect at 14:29 ET today** (`Peer closed connection`) — recurring gateway flakiness, **not caused by today's PRs**. Signal-suppression gate from a prior session fired correctly: `SIGNALS SUPPRESSED: IBKR not live...refusing to generate buy signals on stale fallback data`. Auto-recovery via Docker socket should kick in if reconnect fails 10× in a row.
- **VPS branch drift.** Always confirm `git branch --show-current` AND that the working tree is clean before assuming a deploy landed. A pending diff silently aborts `git checkout` and the next `docker compose build` then bakes the wrong tree into the image.
- **Bar warmup after restart.** Momentum needs ~3.3h of 5-min bars. First trade after `--force-recreate` typically not before noon ET.
- **VNC publicly exposed** on `0.0.0.0:5900` — not addressed in this brief. Bind to `127.0.0.1:5900` in `docker-compose.yml` next session if you want the SSH-tunnel-only access pattern.
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
