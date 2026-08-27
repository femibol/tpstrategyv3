# Handoff Brief — TradingView MCP + Pine Strategy Validation

For a **local** Claude Code session on Femi's machine (the TradingView MCP needs
the locally running TradingView desktop app; cloud sessions cannot reach it).

GOAL
Connect local Claude Code to the TradingView desktop app via tradingview-mcp,
load the three Pine strategies from this folder, backtest them across the
watchlist with walk-forward date windows, commit the results back to this repo,
and create alerts ONLY for strategy/symbol combos that pass the go/no-go gate.

CONTEXT
- Project: personal trading system (tpstrategyv3) — NOT client work, no ticket.
- Repo: github.com/femibol/tpstrategyv3 — open Claude Code in the local clone.
- The VPS bot is PARKED (deliberately — do not try to revive it, do not touch
  the claude/cmd or claude/live-state branches). Current architecture:
  TradingView = signals + backtests; IBKR paper via TradingView panel = fills.
- Strategy scripts (in this folder):
  - tradingview/crypto_mean_reversion.pine   — 5m Coinbase pairs (COINBASE:NEARUSD etc.)
  - tradingview/premarket_gap_scalper.pine   — 1m/5m US equities, EXTENDED HOURS ON
  - tradingview/daily_trend_rider.pine       — DAILY charts, watchlist below
  - (tradingview/algobot_entry_visualizer.pine is older tooling — ignore)
- Each script has: a "Setting chain" preset dropdown, a "Backtest window"
  date-range filter, entry/exit chart labels, and a dashboard table.
- Watchlist (trend rider + general equity): NVDA AMD META TSLA AVGO PLTR COIN
  MSTR HOOD SMCI NFLX UBER SHOP SOFI RKLB ARM CRWD NET DDOG IONQ
- Crypto symbols: NEAR, AAVE, ALGO, LINK, AVAX, DOGE, SOL, ETH (Coinbase, 5m).
- MCP server: github.com/tradesdontlie/tradingview-mcp (community project).
- Background: 5 weeks of paper trading showed equity momentum bleeding
  (-$846 in stops) while crypto mean-reversion held ~breakeven through a
  crypto pullback. PR #256 cut the bot to the proven core. These backtests
  decide what earns live alerts. HANDOFF.md has full history.

CONSTRAINTS
- IBKR **paper** login only in TradingView's trading panel. Never place or
  automate orders on a live account in this workflow.
- Do not modify the .pine strategy logic while testing — presets and date
  windows only. If a script won't compile, fix the compile error minimally
  and note it in the results file.
- Alerts only after a combo passes the gate (see REFERENCE). No alerts on
  anything that fails — a losing strategy with alerts is worse than none.
- tradingview-mcp drives the real TradingView app — if a tool misbehaves,
  stop and report rather than retrying destructive actions.

ACCEPTANCE CRITERIA
- [ ] `tv_health_check` returns connected.
- [ ] All three scripts compile and are saved in TradingView (pine_set_source
      + pine_smart_compile + pine_save; zero compile errors).
- [ ] Backtest matrix completed and committed to
      tradingview/results/backtest-results.md on a feature branch, PR opened:
      trend rider = 20 symbols x 4 presets (full history + 2026-only window);
      gap scalper = 5+ recent gappers x 4 presets;
      crypto MR = 6+ Coinbase pairs x 4 presets (as far back as 5m data allows).
      Each row: symbol, preset, window, trades, win%, PF, net, max DD.
- [ ] Go/no-go verdict per strategy written at the top of the results file.
- [ ] Alerts created (alert_create, "Order fills and alert() function calls")
      ONLY for combos passing the gate; list of created alerts in results file.
- [ ] Nothing pushed to main directly; no bot config files touched.

FIRST ACTIONS
1. Verify prerequisites: TradingView desktop app installed + logged in
   (paid plan needed for alerts; backtesting works on any), Node 18+,
   local clone of this repo up to date (`git pull origin main`).
2. Install the MCP server:
   git clone https://github.com/tradesdontlie/tradingview-mcp.git
   cd tradingview-mcp && npm install
3. Launch TradingView with debugging (from the tradingview-mcp folder):
   Mac: ./scripts/launch_tv_debug_mac.sh | Win: scripts\launch_tv_debug.bat
   (manual fallback: /path/to/TradingView --remote-debugging-port=9222)
4. Register the server in the repo's .mcp.json (do not commit machine paths):
   {"mcpServers":{"tradingview":{"command":"node",
     "args":["<ABSOLUTE PATH>/tradingview-mcp/src/server.js"]}}}
   Restart Claude Code, then run tv_health_check.
5. Load each .pine file from tradingview/ via pine_set_source →
   pine_smart_compile → fix any compile errors minimally → pine_save.
6. Run the backtest matrix (chart_set_symbol + chart_set_timeframe, flip
   presets via indicator settings; read results from the Strategy Tester —
   data_get_pine_tables reads each script's dashboard table, and
   capture_screenshot for anything unreadable programmatically).
   Record every run into tradingview/results/backtest-results.md as you go.
7. Apply the go/no-go gate (REFERENCE), write verdicts, create alerts for
   passers only.
8. Commit results + verdicts to a branch, push, open a PR to main.

REFERENCE
Go/no-go gate per strategy/symbol/preset combo (ALL required):
  - trades >= 30 on the full window (>= 15 acceptable for daily-TF trend rider)
  - Profit Factor >= 1.3 on the full window
  - PF >= 1.0 on the held-out 2026-only window (walk-forward check)
  - Max drawdown <= 15% of the $10k test capital
  - Verdict GO -> create alerts; MARGINAL (one miss) -> note, no alerts;
    NO-GO -> nothing.
Alert payloads: already embedded in every strategy's alert_message
  (JSON: {"ticker","action","price"}) — later reusable for TradersPost
  webhooks; for now alerts are app-push notifications for manual fills.
Chart requirements: gap scalper NEEDS extended hours ON and works best 1m;
  trend rider is DAILY bars; crypto MR is 5m Coinbase pairs.
Intraday history is plan-limited on TradingView — for the gap scalper and
  crypto MR, test as far back as data loads and note the actual window used.
The cloud Claude session independently backtested the trend rider on 2 years
  of IBKR daily bars — compare its results (see repo HANDOFF.md /
  tradingview/results/ once merged) against the TradingView numbers; large
  disagreement means a logic divergence worth flagging, not ignoring.

QUESTIONS
- Which OS is this machine (picks the debug-launch script)?
- Which TradingView plan is active (decides how many alerts we can create
  and how far intraday history reaches)?
- Is the IBKR paper login already connected in TradingView's trading panel?
