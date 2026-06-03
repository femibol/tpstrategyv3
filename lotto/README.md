# Lotto — Anti-Split Number Generator

A small, self-contained tool (no dependencies, stdlib only) for generating
**anti-split** lottery numbers for the WCLC / Western Canada games. It lives in
its own folder and does not touch any trading-bot code.

> **This is not a prediction tool.** Lottery draws are provably random — we
> tested it exhaustively (chi-square on 4,400+ real draws over 44 years, six
> datasets, five backtested "systems", a shuffle test, and a trained ML model
> that scored AUC 0.4935 out-of-sample = a coin flip). No system, math, or AI
> can predict the numbers, because past draws carry zero information about
> future draws.

## What it *does* do

The only mathematically real edge in the lottery is not on the draw — it's on
the **other players**. People pick numbers non-randomly (birthdays, lucky 7,
patterns), so choosing **unpopular** combinations means that *if* you win, you
split the jackpot with fewer people. Same odds of winning; a bigger slice when
you do. The generator builds sets that dodge the documented crowd biases:

- favors numbers **above 31** (escapes birthday / day-of-month picks)
- avoids **7** and downweights **1–12** (the most over-chosen numbers)
- no **consecutive runs** or **arithmetic sequences** (popular slip patterns)
- **spreads** across the full range so it never matches a popular cluster

## Usage

```bash
python3 lotto/generate_numbers.py              # one fresh set for every game
python3 lotto/generate_numbers.py --game 649   # just Lotto 6/49
python3 lotto/generate_numbers.py --game max   # just Lotto Max
python3 lotto/generate_numbers.py --sets 3     # 3 alternative sets per game
```

Games: `daily_grand`, `649`, `max`, `western_649`, `western_max`, or `all`.

## Honest reminders

- The house edge (~50%) **cannot** be beaten. Best big-game jackpot odds:
  **Daily Grand / 6/49**. Best realistic jackpot shot: **Lightning Lotto**.
- Treat it as a fixed entertainment budget you're happy to lose. The smartest
  play is a small lotto stake + investing the rest (positive expected value).
- Skip the EXTRA add-on (~46% return, worse than the base game).
- If it ever stops being fun: **ConnexOntario 1-866-531-2600** (free, 24/7).
