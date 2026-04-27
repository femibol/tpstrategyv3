---
description: Open the bot's web dashboard via Playwright MCP, screenshot it, and summarize what's visible (positions, P&L, alerts, errors)
argument-hint: [url] (defaults to http://localhost:5000)
---

Use the Playwright MCP server (tools prefixed `mcp__playwright__`) to inspect the trading bot's web dashboard.

## Steps

1. **Resolve the URL.** Argument: `$ARGUMENTS`. If empty, default to `http://localhost:5000`. If the user passed a `<vps_ip>:<port>` shape, use it as-is.

2. **Navigate** with `mcp__playwright__browser_navigate`. If it fails (connection refused / timeout), tell the user the dashboard isn't reachable from the current machine and suggest:
   - Checking `docker compose ps` for the trading-bot container
   - SSH-tunneling the VPS port: `ssh -L 5000:localhost:5000 <vps>`
   - Passing the VPS IP directly as the argument

3. **Take a snapshot** with `mcp__playwright__browser_snapshot` (accessibility tree — cheap, structured) for the layout, then `mcp__playwright__browser_take_screenshot` for visual confirmation.

4. **Summarize what's on the page** — focus on these signals (the dashboard renders these per `bot/web/`):
   - Open position count + symbols + per-position unrealized P&L
   - Daily / total P&L
   - Current market regime (BULLISH / SIDEWAYS / BEARISH)
   - Recent signals (last few rows of the signals table)
   - Any red/orange status indicators (IBKR disconnected, signal gate active, rate-limit warnings)
   - Bar warmup status (`bars_warm=N/M`)

5. **Cross-check with disk state** — read the latest few entries from `data/trade_history.json` and compare the dashboard's "open positions" count against actual open trades. Flag any mismatch (often means dual-mode broker desync per CLAUDE.md).

6. **Return a punch list:** what looks healthy, what looks off, suggested next action. Be concrete — reference actual numbers and symbols from the page, not generic descriptions.

## Notes

- The browser runs `--headless --isolated`: clean profile, no UI, no persistent cookies. If the page needs auth, ask the user how they want to handle it (env-var creds, or remove `--isolated` from `.mcp.json` for persistent login).
- First call in a fresh session triggers a one-time Chromium download (~120MB) — that's expected.
- Close the browser context with `mcp__playwright__browser_close` when done so the next slash command starts clean.
