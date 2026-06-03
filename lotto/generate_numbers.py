#!/usr/bin/env python3
"""
Anti-split lottery number generator (WCLC / Western Canada games).

This is NOT a prediction tool. We exhaustively tested whether lottery numbers
can be predicted -- chi-square on 4,400+ real draws across 44 years, six
datasets, five backtested "systems", a shuffle test, and a trained ML model
(out-of-sample AUC = 0.4935, i.e. a coin flip). The draw is provably random:
past draws carry zero information about future draws.

The ONE mathematically real edge is not on the draw -- it's on the other
players. Human number-picking is wildly non-random, so choosing UNPOPULAR
combinations means that *if* you win, you split the jackpot with fewer people.
Same odds of winning; a bigger slice when you do.

This script builds number sets that dodge the documented crowd biases:
  - Numbers >31 are under-picked (no birthdays / days-of-month). Favor them.
  - "Lucky" 7 and low/month numbers (1-12) are over-picked. Avoid/​downweight.
  - Consecutive runs, arithmetic sequences and bet-slip patterns are popular.
    Avoid them.
  - Spread across the whole range so the set never matches a popular cluster.

Sources: Henze & Riedwyl, "How to Win More"; Cook & Clotfelter (1993),
Management Science; University of Southampton popular-number study.

Usage:
    python3 lotto/generate_numbers.py            # one fresh set for every game
    python3 lotto/generate_numbers.py --game 649 # just one game
    python3 lotto/generate_numbers.py --sets 3   # 3 alternative sets per game
"""
import argparse
import random

POPULAR = {7}  # most over-chosen single number


def antisplit(count, lo, hi, high_cut=31, min_gap=2, rng=random):
    """Pick `count` distinct numbers from [lo, hi] biased toward unpopular ones."""
    pool = list(range(lo, hi + 1))

    def weight(n):
        w = 5.0 if n > high_cut else 1.0   # favor numbers above the birthday cutoff
        if n in POPULAR:
            w = 0.2                          # avoid the single most-picked number
        if n <= 12:
            w *= 0.6                          # months are extra over-picked
        return w

    while True:
        picks, cand = [], pool[:]
        for _ in range(count):
            if not cand:
                break
            x = rng.choices(cand, weights=[weight(n) for n in cand], k=1)[0]
            picks.append(x)
            cand = [n for n in cand if abs(n - x) >= min_gap]  # kill clusters
        if len(picks) == count:
            picks.sort()
            diffs = {picks[i + 1] - picks[i] for i in range(len(picks) - 1)}
            if len(diffs) > 1:               # reject constant-step sequences
                return picks


def high_share(picks):
    return sum(1 for n in picks if n > 31)


# (name, generator-callable returning a formatted line)
GAMES = {
    "daily_grand": lambda rng: (
        "DAILY GRAND",
        antisplit(5, 1, 49, rng=rng),
        f"+ Grand# {rng.choice([2, 3, 4, 5, 6, 7])}",
    ),
    "649": lambda rng: ("LOTTO 6/49", antisplit(6, 1, 49, rng=rng), ""),
    "max": lambda rng: ("LOTTO MAX", antisplit(7, 1, 52, rng=rng), ""),
    "western_649": lambda rng: ("WESTERN 649", antisplit(6, 1, 49, rng=rng), ""),
    "western_max": lambda rng: ("WESTERN MAX", antisplit(7, 1, 50, rng=rng), ""),
}


def main():
    ap = argparse.ArgumentParser(description="Anti-split lottery number generator")
    ap.add_argument("--game", choices=list(GAMES) + ["all"], default="all")
    ap.add_argument("--sets", type=int, default=1, help="alternative sets per game")
    args = ap.parse_args()
    rng = random.Random()  # fresh entropy every run

    games = list(GAMES) if args.game == "all" else [args.game]
    print("=" * 60)
    print("  ANTI-SPLIT LOTTERY NUMBERS")
    print("  (same odds to win; fewer people to split with if you do)")
    print("=" * 60)
    for g in games:
        print()
        for _ in range(args.sets):
            name, nums, extra = GAMES[g](rng)
            tail = f"  ({high_share(nums)}/{len(nums)} above 31)"
            print(f"  {name:<12}: {nums} {extra}{tail}")
    print("\n  Reminder: fixed fun-budget only. The house edge (~50%) cannot be")
    print("  beaten. If it stops being fun: ConnexOntario 1-866-531-2600.")


if __name__ == "__main__":
    main()
