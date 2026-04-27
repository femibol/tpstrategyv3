---
description: Find recent rejected signals + failed webhooks + rate-limit hits and group them by reason with examples
argument-hint: [days] (defaults to 3)
---

Surface the signal-rejection picture for debugging. Match the CLAUDE.md "Common Issues" list — TradersPost rejections, "no open position" exits, rate limits, dual-mode mismatches.

## Steps

1. **Window.** Argument `$ARGUMENTS` (default `3`) is the lookback in days.

2. **Read `data/signal_log.json`** — the last 2000 signals are stored. Filter to the window. For each rejected signal, capture: timestamp, symbol, action (buy/sell/exit), strategy, HTTP status, response body excerpt, rejection_reason if stamped.

3. **Grep `logs/trading.log`** within the window for these patterns and count occurrences:
   - `REJECTED` — risk manager / position cap / score gate
   - `RATE LIMIT` — 3s global cooldown / 3-per-60s per-symbol cap
   - `no open position` — exit signal for position TradersPost doesn't know about (dual-mode mismatch per CLAUDE.md)
   - `IBKR NOT CONNECTED` — broker outage masking signals
   - `Yahoo fallback` — running on stale data

4. **Group + report.** Output format:

   ```
   ## Last <N> Days — <total signals sent> sent, <accepted> accepted, <rejected> rejected

   ## Rejection Breakdown
   - <reason>: <count> — <one-line interpretation>
     Example: <symbol> @ <time> — <excerpt>

   ## Webhook Failures (HTTP non-2xx)
   - <count> failures across <count> symbols
   - Top symbols: <sym1> (N), <sym2> (M)

   ## Rate Limit Hits
   - Global 3s cooldown: <count>
   - Per-symbol 3-per-60s: <count>

   ## Connectivity Masking
   - IBKR NOT CONNECTED: <count> events
   - Yahoo fallback signals: <count> (these should be zero post-PR #117 signal gate)

   ## Probable Root Causes (ranked)
   1. <hypothesis tied to specific reasons + counts>
   2. ...
   ```

5. **Flag anomalies** — if Yahoo-fallback signals are nonzero, the PR #117 signal-gate has regressed; surface that as a P0. If TradersPost dual-mode mismatches exist, suggest the next session check `bot/brokers/traderspost.py` for the broker-side desync.
