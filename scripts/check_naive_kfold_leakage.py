#!/usr/bin/env python3
"""Quantify naive-KFold leakage in trade_history.json.

Sanity check before using sklearn's standard KFold on trade data.
Walks `data/trade_history.json`, computes how many training rows
would be informationally contaminated by test-set information under
a naive contiguous split, and shows what PurgedKFold drops to fix it.

Run from repo root:
    python3 scripts/check_naive_kfold_leakage.py
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from bot.learning.purged_validation import (  # noqa: E402
    PurgedKFold,
    count_naive_leakage,
    samples_from_trades,
)


def main() -> int:
    trades_path = REPO / "data" / "trade_history.json"
    if not trades_path.exists():
        print(f"ERROR: {trades_path} not found", file=sys.stderr)
        return 1

    with trades_path.open() as f:
        trades = json.load(f)
    samples = samples_from_trades(trades)
    print(f"trades with valid timestamps: {len(samples)}\n")

    print(f"{'n_splits':>9s}  {'naive leak (rows)':>18s}  {'leak %':>8s}")
    print("-" * 42)
    for n_splits in (3, 5, 10):
        leaks = count_naive_leakage(samples, n_splits=n_splits)
        # Approximate total train rows across all folds under naive split.
        total_train = len(samples) * (n_splits - 1)
        pct = leaks / max(1, total_train) * 100
        print(f"{n_splits:>9d}  {leaks:>18d}  {pct:>7.1f}%")

    n_splits = 5
    print(f"\nPer-fold breakdown (n_splits={n_splits}):")
    print(f"{'fold':>4s} {'test_n':>7s} {'purged_n':>9s} "
          f"{'embargo_n':>10s} {'rows_dropped':>13s}")
    cv = PurgedKFold(n_splits=n_splits, samples=samples, embargo_frac=0.0)
    cv_e = PurgedKFold(n_splits=n_splits, samples=samples, embargo_frac=0.02)
    folds_p = list(cv.split())
    folds_e = list(cv_e.split())
    for k, ((tp, te), (tpe, _)) in enumerate(zip(folds_p, folds_e)):
        naive_n = len(samples) - len(te)
        dropped = naive_n - len(tp)
        embargo_extra = len(tp) - len(tpe)
        print(f"{k:>4d} {len(te):>7d} {len(tp):>9d} "
              f"{len(tpe):>10d} {dropped:>10d}+{embargo_extra}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
