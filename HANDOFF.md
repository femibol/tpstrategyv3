# Session Handoff

Current state of in-progress work so the next Claude Code session picks up without re-deriving context. Update this file **before** the session maxes out.

---

## Last Updated
2026-04-17 — dashboard cleanup + no-trades diagnostics

## Recently Shipped (merged to main)
- **PR #103** — Dashboard: removed redundant bottom bar. Frees ~80px of mobile viewport.
- **PR #102** — Dashboard overhaul: live feed, control buttons, positions-first tabs.
- **PR #101** — IBKR is source of truth for capital.
- **PR #100** — PineScript clean defaults.

## Open / In Progress (branch: `claude/code-session-work-CaCgk`)
- **Cycle heartbeat + IBKR-primary log fix** — pushed but NOT yet merged/deployed. Commits:
  - `d35e58b` — fix misleading "SIMULATED" warning (IBKR is the real broker)
  - `(next)` — add `CYCLE #N:` INFO log every ~1 min with regime/signals/approved/positions/bars_warm/market-state + warmup hint when most bars cold. This will make "why no trades" diagnose-at-a-glance.
- **Deploy pending on VPS** for PRs #102, #103, and the heartbeat branch. Rebuild:
  ```bash
  cd /opt/trading-bot && git pull && docker compose build --no-cache trading-bot && docker compose up -d --force-recreate trading-bot
  ```

## Why No Trades (diagnosis so far)
Bot is running, IBKR connected, 94 symbols streaming, but 0 trades. Investigation findings:
1. **Most likely — bar warmup.** `momentum.py:56` rejects if `<40` 5-min bars. Every `--force-recreate` wipes the buffer → silent for ~3.3h.
2. **Regime=crisis blocker** at `engine.py:1171` silently drops new buys. No current log.
3. **Market hours** — only 9:32-15:50 ET. First 2 min / last 10 min skipped.
4. **DEBUG-level rejections** in strategies (`momentum.py:49`) are invisible at INFO.

Also found: **logs/data were in Docker named volumes** — persisted across rebuilds, but NOT readable from the host filesystem. That's why `tail logs/trading.log` from `/opt/trading-bot` always failed. Switched to bind mounts in `docker-compose.yml` this session.

### Deploy migration (one-time, BEFORE next rebuild)
Copy old named-volume data to the new host-bind locations so you keep the history:
```bash
cd /opt/trading-bot
mkdir -p data logs
docker run --rm -v trading-bot_bot-logs:/from -v $(pwd)/logs:/to alpine sh -c "cp -a /from/. /to/ 2>/dev/null || true"
docker run --rm -v trading-bot_bot-data:/from -v $(pwd)/data:/to alpine sh -c "cp -a /from/. /to/ 2>/dev/null || true"
git pull && docker compose build --no-cache trading-bot && docker compose up -d --force-recreate trading-bot
tail -f logs/trading.log   # finally works from the host!
```

## Next Up
- Bump strategy-level rejection logs from DEBUG to INFO (or configurable).
- Verify heartbeat output after first deploy — confirms no-trades diagnosis.
- PR #41 stale — verify or close.

## Known Gotchas / Watch-outs
- Logs inside container — not on host. Use `docker compose exec trading-bot tail logs/trading.log`.
- Bottom-bar CSS (`.controls`, `.ctrl-btn.*`) in `dashboard.html` (~125-145, ~411-420) is now dead code.
- IB Gateway health check was `unhealthy` in last user output — may need reconnect (button on dashboard).

## How to Use This File
- **Start of session**: read this first, then git log to confirm.
- **End of session**: update "Last Updated", move merged items to "Recently Shipped", record new open work, push to the working branch.
