"""Purged + embargoed walk-forward CV (López de Prado ch. 7).

Standard k-fold leaks future information into training when labels span
multiple periods: a trade that enters in fold 1 and exits in fold 2 puts
fold-2 information into a fold-1 training row. The fix has two pieces:

1. PURGE: drop any training sample whose label window (entry_time →
   exit_time) overlaps the test window. The overlap *is* the leak.
2. EMBARGO: after the test window, skip N samples before resuming
   training. Defends against subtle leakage from autocorrelated
   features (e.g. yesterday's regime feature predicts today's).

Use this whenever you tune a parameter, fit a meta-label model, or
evaluate a strategy variant on `trade_history.json`. Naive
sklearn.KFold on time-series data systematically over-estimates edge.

API mirrors sklearn's BaseCrossValidator so it drops into existing
sklearn pipelines: `PurgedKFold(...).split(X)` yields `(train_idx,
test_idx)` arrays.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Iterator, Sequence

import numpy as np


@dataclass
class Sample:
    """One labeled training row with the time window its label covers.

    For trade-based labels, `start` is the entry time and `end` is the
    exit time. For feature-based labels with a forward-looking horizon,
    `start` is the feature snapshot time and `end = start + horizon`.
    """
    start: datetime
    end: datetime

    def overlaps(self, other: "Sample") -> bool:
        """True if the two windows share any time. Standard interval
        overlap: not (one ends before the other begins)."""
        return not (self.end < other.start or other.end < self.start)


class PurgedKFold:
    """Time-series k-fold cross-validator with purging and embargo.

    Parameters
    ----------
    n_splits : int
        Number of folds. Each fold becomes a test set in turn.
    samples : Sequence[Sample]
        One entry per training row, in the same order as your feature
        matrix X. Provides the label time-window used for purging.
    embargo_frac : float, default 0.0
        Fraction of total samples to drop from training AFTER each test
        fold. 0.01 = 1% embargo. De Prado recommends 1-2% for daily
        data with multi-day labels.

    Notes
    -----
    Splits are contiguous, ordered: fold 0 is the earliest block of
    samples, fold k-1 is the latest. This matches walk-forward intent —
    you never test on data older than your training.
    """

    def __init__(
        self,
        n_splits: int,
        samples: Sequence[Sample],
        embargo_frac: float = 0.0,
    ):
        if n_splits < 2:
            raise ValueError(f"n_splits must be >= 2, got {n_splits}")
        if not 0 <= embargo_frac < 1:
            raise ValueError(f"embargo_frac must be in [0, 1), got {embargo_frac}")
        if len(samples) < n_splits:
            raise ValueError(
                f"len(samples)={len(samples)} < n_splits={n_splits} — "
                f"each fold needs at least one sample"
            )
        self.n_splits = n_splits
        self.samples = list(samples)
        self.embargo_frac = embargo_frac

    def get_n_splits(self, X=None, y=None, groups=None) -> int:
        return self.n_splits

    def split(
        self, X=None, y=None, groups=None
    ) -> Iterator[tuple[np.ndarray, np.ndarray]]:
        """Yield (train_idx, test_idx) for each fold.

        Both arrays are integer indices into self.samples (and X / y if
        passed). Training indices exclude:
          - the test window itself
          - any sample whose label window overlaps the test window (purge)
          - any sample within embargo_frac × n after the test window
        """
        n = len(self.samples)
        embargo_n = int(round(self.embargo_frac * n))
        # Contiguous fold boundaries: roughly equal-sized blocks.
        fold_edges = np.linspace(0, n, self.n_splits + 1, dtype=int)
        all_idx = np.arange(n)

        for k in range(self.n_splits):
            test_start, test_end = fold_edges[k], fold_edges[k + 1]
            test_idx = all_idx[test_start:test_end]
            if test_idx.size == 0:
                continue

            test_window_start = min(self.samples[i].start for i in test_idx)
            test_window_end = max(self.samples[i].end for i in test_idx)
            test_window = Sample(start=test_window_start, end=test_window_end)

            # Candidate training set: everything outside the test block.
            candidates = np.concatenate([
                all_idx[:test_start],
                all_idx[test_end:],
            ])

            # Purge: drop any training sample whose label window overlaps
            # the test window.
            keep = np.ones(candidates.size, dtype=bool)
            for i_local, i_global in enumerate(candidates):
                if self.samples[i_global].overlaps(test_window):
                    keep[i_local] = False

            # Embargo: drop training samples whose POSITION-INDEX falls
            # within embargo_n after the test fold ends. (De Prado's
            # canonical form — based on row index, not on time, so it's
            # invariant to clock drift.)
            if embargo_n > 0:
                embargo_zone_end = test_end + embargo_n
                for i_local, i_global in enumerate(candidates):
                    if test_end <= i_global < embargo_zone_end:
                        keep[i_local] = False

            train_idx = candidates[keep]
            yield train_idx, test_idx


def samples_from_trades(trades: list[dict]) -> list[Sample]:
    """Build Sample windows from a `trade_history.json`-shaped list.

    Each trade contributes one window from entry_time → exit_time.
    Skips trades missing either timestamp. Returned in input order so
    indices align with the source list.
    """
    out: list[Sample] = []
    for t in trades:
        et = t.get("entry_time")
        xt = t.get("exit_time")
        if not et or not xt:
            continue
        try:
            start = datetime.fromisoformat(et)
            end = datetime.fromisoformat(xt)
        except (ValueError, TypeError):
            continue
        if end < start:
            # Defensive: a malformed record shouldn't crash the split.
            end = start
        out.append(Sample(start=start, end=end))
    return out


def count_naive_leakage(samples: Sequence[Sample], n_splits: int) -> int:
    """Diagnostic: how many naive-split training rows would leak into the
    test set across all folds? Returns total overlap count. Use this
    against your real data to make the case for purging — if the number
    is 0, naive k-fold is fine; if it's large, you've been over-fitting.
    """
    n = len(samples)
    fold_edges = np.linspace(0, n, n_splits + 1, dtype=int)
    leaks = 0
    for k in range(n_splits):
        ts, te = fold_edges[k], fold_edges[k + 1]
        if ts == te:
            continue
        tw_start = min(samples[i].start for i in range(ts, te))
        tw_end = max(samples[i].end for i in range(ts, te))
        tw = Sample(start=tw_start, end=tw_end)
        for i in range(n):
            if ts <= i < te:
                continue
            if samples[i].overlaps(tw):
                leaks += 1
    return leaks
