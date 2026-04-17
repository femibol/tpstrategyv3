# Session Handoff

Current state of in-progress work so the next Claude Code session picks up without re-deriving context. Update this file **before** the session maxes out.

---

## Last Updated
2026-04-17 — dashboard cleanup session

## Recently Shipped (merged to main)
- **PR #103** — Dashboard: removed redundant bottom bar (Ideas/RVOL/Runners/Pause/Close/STOP). Top control bar + tab navigation cover the same actions. Frees ~80px of mobile viewport.
- **PR #102** — Dashboard overhaul: live feed, control buttons, positions-first tabs.
- **PR #101** — IBKR is source of truth for capital (prevents false drawdown stop).
- **PR #100** — PineScript clean defaults (noise off, trades visible).

## Open / In Progress
- **Deploy pending on VPS.** Latest dashboard changes (PR #102, #103) need the rebuild step below to be visible in the browser:
  ```bash
  cd /opt/trading-bot
  docker compose build --no-cache trading-bot
  docker compose up -d --force-recreate trading-bot
  ```
  Then hard-refresh the browser (iPhone: hold reload → Request Desktop Site, or close/reopen tab).
- **PR #41** (`claude/algo-trading-bot-srXXf`) — very old, likely stale. Verify relevance or close.

## Next Up / Ideas
- _(none queued — ask user)_

## Known Gotchas / Watch-outs
- Docker caches layers; without `--no-cache` a rebuild will skip `COPY .` if `requirements.txt` didn't change → old dashboard persists.
- Bottom-bar CSS (`.controls`, `.ctrl-btn.*`) in `bot/dashboard/templates/dashboard.html` (lines ~125-145, ~411-420) is now dead code. Left in place for now — safe to remove in a follow-up.

## How to Use This File
- **Start of session**: read this first, then git log to confirm.
- **End of session**: update "Last Updated", move merged items to "Recently Shipped", record new open work, push to the working branch.
