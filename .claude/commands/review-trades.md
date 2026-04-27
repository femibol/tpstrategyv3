---
description: Run the CLAUDE.md review checklist — win rate, strategy P&L, rejected signals, regime breakdown — from trade_history.json and the logs
argument-hint: [days] (defaults to 7)
---

Review the trading bot's recent performance. Match what CLAUDE.md "Review Checklist" describes, but produce a single structured report instead of a step-by-step walk.

## Steps

1. **Window.** Argument `$ARGUMENTS` (default `7`) is the lookback in days. Filter all data to trades and signals within that window.

2. **Delegate the heavy reads to the `trade-reviewer` subagent** to keep this main session's context window clean. Ask it to read `data/trade_history.json` + `data/signal_log.json` + `logs/trading.log` and return:
   - Win rate, total P&L, profit factor (sum of wins / sum of losses)
   - P&L by strategy (which strategies are net positive vs net negative)
   - P&L by symbol (top 5 winners, top 5 losers)
   - P&L by hour-of-day (look for time-of-day patterns)
   - Regime distribution + win rate per regime
   - Rejected signals — count by reason, with examples
   - Any dual-mode mismatches in `signal_log.json` (TradersPost rejections from the CLAUDE.md "Common Issues" list)

3. **Cross-check with execution health** — grep `logs/trading.log` for `IBKR NOT CONNECTED`, `BACKGROUND RECONNECT`, `gateway` issues in the window. If counts are nonzero, surface alongside the P&L numbers (a "good" win rate during connectivity outages may be misleading).

4. **Return the report** in this shape:

   ```
   ## Last <N> Days
   <one-sentence headline — e.g. "12 trades, 58% wins, +$340 net, momentum dominant">

   ## What's Working
   - <strategy / symbol / pattern with numbers>

   ## What's Not Working
   - <strategy / symbol / pattern with numbers>

   ## Rejected Signals
   - <reason>: <count> (e.g. "rate limit: 4, dual-mode mismatch: 2")

   ## Execution Health
   - <connectivity / gateway / rate-limit issues with counts>

   ## Top 3 Actions
   1. <specific change with reasoning>
   2. ...
   3. ...
   ```

5. **Don't invent numbers.** If a section has no data, say "no data in window" rather than synthesizing.
