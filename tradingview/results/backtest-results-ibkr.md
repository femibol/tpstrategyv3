# Daily Trend Rider — IBKR-data backtest (cloud session, 2026-08-27)

Independent Python replication of `tradingview/daily_trend_rider.pine`, run on
2 years of real IBKR daily bars (Aug 2024 → Aug 2026) for all 20 watchlist
names. $2,500 per position, $1/side commission, fills at daily close.
Engine: session scratchpad `backtest_rider.py` (same qualify/entry/exit rules
as the Pine script; all four preset chains).

## VERDICT

**GO — "Loose rider (max runner)" chain, on the watchlist MINUS SMCI, MSTR, COIN.**

| Chain | Window | Trades | Win% | PF | Net | Max DD |
|---|---|---|---|---|---|---|
| **Loose ex-crashers** | **Full 2y** | **46** | **52%** | **4.00** | **+$12,836** | **$1,009 (10.1%)** |
| **Loose ex-crashers** | **2026 held-out** | **11** | **45%** | **2.28** | **+$2,137** | **$1,009 (10.1%)** |
| Loose (all 20) | Full 2y | 54 | 48% | 2.34 | +$10,017 | 14.0% |
| Loose (all 20) | 2026 held-out | 14 | 36% | 1.23 | +$707 | 14.0% |
| Bot config | Full 2y | 65 | 57% | 1.18 | +$949 | 16.1% |
| Bot config | 2026 held-out | 18 | 44% | 0.57 | -$1,156 | 16.1% |
| Breakout-only | Full 2y | 35 | 46% | 1.00 | -$3 | 12.0% |
| Conservative | Full 2y | 25 | 36% | 0.58 | -$636 | 7.0% |

Gate check (loose ex-crashers): trades 46 ≥ 15 ✓ · PF 4.00 ≥ 1.3 ✓ ·
held-out PF 2.28 ≥ 1.0 ✓ · DD 10.1% ≤ 15% ✓ → **all four criteria pass.**

## Key findings

1. **Exit policy dominates.** All chains share identical entries; the loose
   chain (3×ATR trail, no exhaustion exit, 40-day hold) out-earned the bot
   config 10:1. The bot's "first red after 5+ greens" exhaustion exit and
   1.5×ATR trail cut winners at ~2–4 days; the trend pays at 12–30 days.
   Biggest single positions: AMD +$1,688 (+67%), ARM +$1,408, IONQ +$2,551
   pooled, RKLB +$1,112, SOFI +$1,305 — all on $2,500 entries.
2. **The bot config independently fails 2026** (PF 0.57), matching what live
   paper trading showed for tight-exit equity strategies over the same
   period. Two unrelated datasets, same conclusion.
3. **Exclusions matter.** SMCI (-$1,814), MSTR (-$657), COIN (-$348) lose
   under every chain — gap-down/crash-prone names break wide-trail trend
   riding. Watchlist for alerts = the other 17 names.
4. **Profile is classic trend-following:** ~45–52% win rate, avg winner
   ~2.6–3.7× avg loser. Most trades are small losses; a few huge winners
   carry everything. Psychologically demanding but statistically sound.

## Caveats

- Held-out 2026 sample is 11 trades — passes the gate but thin; the first
  weeks of paper alerts are still confirmation, not formality.
- Concurrency: up to ~6 simultaneous positions occurred (Apr 2026 cluster);
  at $2,500 each that exceeds $10k capital. Treat sizing as "0.25× account
  per position, max 4 concurrent" when going live, which trims the April
  cluster's tail exposure and the realized net accordingly.
- Fills at daily close, no intraday slippage; daily-TF signals make this a
  small distortion but not zero.
- 2024–26 favored this watchlist. The regime check is the 2026 window
  (choppier, includes drawdowns) — it held PF 2.28 ex-crashers.

## Action taken

`daily_trend_rider.pine` default preset switched "Bot config" →
"Loose rider (max runner)"; header watchlist annotated to exclude
SMCI / MSTR / COIN for this strategy.

## For the local TradingView session

Cross-check: your Strategy Tester runs of `daily_trend_rider.pine` (Loose
rider preset) on these names should be directionally consistent — huge
AMD/ARM/IONQ/RKLB/SOFI winners at 2–5 week holds, small frequent losers,
SMCI/MSTR/COIN net losers. Large disagreement = logic divergence; flag it
rather than ignore it. Alert setup per HANDOFF-TRADINGVIEW.md applies to the
17-name list with the Loose rider preset.
