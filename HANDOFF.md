# Session Handoff

Brief for the next Claude Code session. Read this first, then `git log --oneline -10` + `git branch --show-current`.

---

## Last Updated
2026-05-10 (4) — Final end-of-session. **All 7 brief items + 2 follow-up fixes shipped today.** PR 7 still deferred.

## Today's Merges (in order)

| PR | What | SHA |
| --- | --- | --- |
| #131 | DNS pin (`8.8.8.8`/`1.1.1.1`) on `ib-gateway` | `a2201d8` |
| #132 | Yahoo / yfinance fallback gating + 60s rate limit + logger suppression | `09bcf0e` |
| #133 | Real `README.md` (was 9-byte stub) | `a92da1c` |
| #134 | Mid-session HANDOFF status update | `ac7cfb6` |
| #135 | Dashboard auth hardening + TradingView webhook tighten | `7148b55` |
| #136 | `tests/` scaffold + 59 unit tests + GH Actions | `b42c9d0` |
| #137 | `requirements.txt` pinned `>=` → `==` from production `pip freeze` | `0e48c6f` |
| #138 | Mid-session HANDOFF refresh | `ac12b51` |
| #139 | **Fix auto-recovery never firing on mid-flight IBKR disconnects** | `28a37a3` |
| #140 | **Bind VNC port 5900 to localhost** | `dbe11c2` |

(7 from the original brief + 2 follow-ups discovered during the session.)

## Live State Snapshot (after the day's deploys)
- `main` at `dbe11c2` (or above this handoff once merged).
- VPS: `0e48c6f` is what's actually deployed to the running container as of last verify (PRs 1–4, 6 live; PRs 5, 139, 140 still pending re-deploy).
- Bot last verified: 8 strategies loaded, $24,584.51 paper balance, 2 long positions synced, 95 IBKR streams, IBKR connected via paper port 4002.
- DASHBOARD_SECRET_KEY rotated to a real random value (verified PR #135 returns 401 on unauthenticated curl).

## Required redeploy on the VPS
PRs **#135 (auth)**, **#139 (auto-recovery fix)**, and **#140 (VNC localhost)** all need a rebuild + recreate to take effect. After that the running image will match `main`.

```bash
cd /opt/trading-bot
git status                                              # expect clean (or only local hand-patches)
git pull origin main
git log --oneline -1                                    # expect dbe11c2 or newer
docker compose build trading-bot
docker compose up -d --force-recreate trading-bot
sleep 15
docker compose logs trading-bot --tail=30 | grep -iE 'dashboard|ibkr|runtime|error|connected|trading engine'
```

After the deploy, sanity:
```bash
NEW_SECRET=$(grep '^DASHBOARD_SECRET_KEY=' .env | cut -d= -f2)
curl -i -s http://localhost:5000/api/positions | head -1                          # MUST be 401
curl -i -s -u admin:"$NEW_SECRET" http://localhost:5000/api/positions | head -1   # 200
nmap -p 5900 50.116.54.226 2>/dev/null | tail -3                                  # expect closed/filtered
```

## What's Next (open items)

### PR 7 — Split `bot/engine.py` (8 632 lines) via mixins — DEFERRED
- Own session, after market close, manual paper-verify required, **do not auto-merge**.
- See PR #130's body for the mixin scaffold spec; HANDOFF prior versions (in git log) have the 3 verify checks.
- Risk: the `_execute_signal` defense-in-depth gate stack and the freshly-fixed reconnect plumbing (PR #139) are load-bearing. Refactor must preserve every guard.

### Auto-recovery fix verification — needs first real gateway flake
PR #139 fix is integration-tested by today's outage being the trigger, but the new escalation path (`Fast-path reconnect failed; escalating to background reconnect` → `BACKGROUND RECONNECT: attempt #N` → `AUTO-RECOVERY: restarting ib-gateway container`) hasn't fired live yet. Next gateway drop should produce these log lines within ~7.5 min of disconnect, plus a Discord `@everyone` ping. If it doesn't, dig into:
- `_health_check` schedule — confirm it actually fires every ~5 min (look for the AP scheduler config)
- `broker.is_connected()` semantics — make sure it returns False quickly enough on a wedge

### IBKR API ports 4001 / 4002 still public
PR #140 only locked down VNC. The IBKR API ports are still on `0.0.0.0`. There's no host-side caller (trading-bot reaches the gateway via shared netns), so binding to `127.0.0.1` would harden them with zero functional impact. ~3-line PR if/when you want it.

### Front-end dashboard JS still references unused `AUTH_KEY`
`bot/dashboard/templates/dashboard.html:1107` reads `{{ dashboard_key }}` and sends it as `X-API-Key`. After PR #135, `dashboard_key=""` is rendered and the global Basic-auth hook ignores `X-API-Key`. Cleanup PR: drop the JS constant + the template arg + the `dashboard_key=""` placeholder in `bot/dashboard/app.py`.

## Recently Shipped (merged earlier in repo history)
- **#129** TradersPost as execution fallback when IBKR is wedged.
- **#128** Stripped a stray `nest_asyncio.apply()`.
- **#126** Migrated `ib_insync` → `ib_async`.
- **#127** Pinned `gnzsnz/ib-gateway` to `10.37.1r`.
- **#125** Downgraded base image to `python:3.10-slim`.
- **#124** Pinned `nest_asyncio>=1.6.0`.

## Gotchas (carry-forward + new today)
- **VPS branch drift.** Always confirm `git branch --show-current` AND that the working tree is clean before assuming a deploy landed. A pending diff silently aborts `git checkout`, then `docker compose build` bakes the wrong tree into the image. We hit this twice today — once on the auth verify, once on the post-merge deploy.
- **Dashboard refuses to start with empty `DASHBOARD_SECRET_KEY`.** Intentional fail-closed (PR #135). Set it before any redeploy.
- **`AUTH_KEY` placeholder** — see open item above. Don't ship `verify-only-123` or `change-this-to-random-string` as real secrets; rotate to a random hex string.
- **Bar warmup after restart.** Momentum needs ~3.3h of 5-min bars. First trade after `--force-recreate` typically not before noon ET.
- **IBKR Gateway flakiness recurs every few hours** (`Peer closed connection`). Until PR #139 is deployed and verified, the fallback is `docker compose up -d --force-recreate trading-bot`. After PR #139 deploys, expect auto-recovery within ~7.5 min.
- **`engine.py` is 8 632 lines.** Any edit risks merge conflicts with PR 7 once it lands. If touching `engine.py` before PR 7, plan to rebase PR 7 on top.
- **PR 139's reconnect rework**: there are now TWO reconnect paths (fast `broker.reconnect()` + background thread). The fast path runs ~2.5 min; on failure it spawns the background path. Total time to first AUTO-RECOVERY = ~7.5 min from disconnect. Acceptable but could be tightened by skipping the fast path on subsequent failures.

## Trade Data Locations (from CLAUDE.md)
- `data/trade_history.json` — every closed trade (bind-mounted to host).
- `data/signal_log.json` — every TradersPost webhook signal.
- `logs/trading.log` — main bot log.
- `logs/trades.log` — trade-only log.

## How to Use This File
- **Start of session**: read this first, then `git log --oneline -10` + `git branch --show-current`.
- **End of session**: update "Last Updated", move merged items to "Recently Shipped", record open work, push to the working branch.
