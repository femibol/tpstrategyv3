"""PurgedKFold — correctness of purging + embargo.

Critical invariants:
1. No test sample appears in its own training set.
2. Splits are contiguous + ordered (walk-forward, not random).
3. Any training sample whose label window overlaps the test window is
   dropped (purging).
4. Training samples within `embargo_frac × n` rows after the test fold
   are dropped (embargo).
5. With `embargo=0` and non-overlapping labels, behaves like KFold
   over contiguous time blocks.
"""
from __future__ import annotations

from datetime import datetime, timedelta

import numpy as np
import pytest

from bot.learning.purged_validation import (
    PurgedKFold,
    Sample,
    count_naive_leakage,
    samples_from_trades,
)


def _seq(n, start_iso="2026-01-01", step_minutes=10, label_minutes=10):
    """Build n samples spaced step_minutes apart with label_minutes long
    label windows (so each label ends exactly when the next begins —
    edge case: touching but not overlapping)."""
    base = datetime.fromisoformat(start_iso)
    return [
        Sample(start=base + timedelta(minutes=i * step_minutes),
               end=base + timedelta(minutes=i * step_minutes + label_minutes))
        for i in range(n)
    ]


# ---------- Sample.overlaps ----------

def test_overlap_disjoint_is_false():
    a = Sample(datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 1, 0, 5))
    b = Sample(datetime(2026, 1, 1, 0, 10), datetime(2026, 1, 1, 0, 15))
    assert not a.overlaps(b)
    assert not b.overlaps(a)


def test_overlap_touching_endpoints_counts_as_overlap():
    """Conservative: end == start counts as overlap. Strict half-open
    intervals would say otherwise, but in trade-data context a trade
    that exits exactly when the next enters is informationally linked."""
    a = Sample(datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 1, 0, 10))
    b = Sample(datetime(2026, 1, 1, 0, 10), datetime(2026, 1, 1, 0, 20))
    assert a.overlaps(b)


def test_overlap_nested_is_true():
    outer = Sample(datetime(2026, 1, 1, 0, 0), datetime(2026, 1, 1, 1, 0))
    inner = Sample(datetime(2026, 1, 1, 0, 20), datetime(2026, 1, 1, 0, 40))
    assert outer.overlaps(inner)


# ---------- PurgedKFold split mechanics ----------

def test_invalid_n_splits_raises():
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=1, samples=_seq(10))


def test_invalid_embargo_raises():
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=3, samples=_seq(10), embargo_frac=1.0)
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=3, samples=_seq(10), embargo_frac=-0.1)


def test_too_few_samples_raises():
    with pytest.raises(ValueError):
        PurgedKFold(n_splits=5, samples=_seq(3))


def test_split_count_matches_n_splits():
    cv = PurgedKFold(n_splits=4, samples=_seq(20))
    assert sum(1 for _ in cv.split()) == 4


def test_test_indices_are_contiguous_and_ordered():
    """Walk-forward: fold 0 holds the earliest samples, fold k-1 the
    latest. Within each fold, indices are a contiguous block."""
    cv = PurgedKFold(n_splits=4, samples=_seq(20))
    prev_max = -1
    for train_idx, test_idx in cv.split():
        # contiguous
        assert (np.diff(test_idx) == 1).all()
        # strictly later than the previous fold
        assert test_idx.min() > prev_max
        prev_max = test_idx.max()


def test_no_test_index_appears_in_train():
    cv = PurgedKFold(n_splits=4, samples=_seq(20))
    for train_idx, test_idx in cv.split():
        assert set(train_idx).isdisjoint(set(test_idx))


# ---------- Purging ----------

def test_purging_drops_overlapping_train_sample():
    """One long-label trade spans the entire test fold → must be purged."""
    base = datetime(2026, 1, 1)
    samples = [
        Sample(base + timedelta(minutes=i * 10),
               base + timedelta(minutes=i * 10 + 5))
        for i in range(10)
    ]
    # Sample 0 has a label that extends WAY into the future, covering folds 1+
    samples[0] = Sample(base, base + timedelta(hours=2))
    cv = PurgedKFold(n_splits=2, samples=samples)
    folds = list(cv.split())
    # In fold 1 (test = samples 5-9), sample 0's label window overlaps
    # the test window. It must be PURGED from the training set.
    train_idx, test_idx = folds[1]
    assert 0 not in train_idx, (
        f"sample 0 (label window covers test fold) must be purged; "
        f"got train_idx={train_idx.tolist()}"
    )


def test_no_purge_when_labels_dont_overlap():
    """Tight non-overlapping labels (step=10m, label=5m) → no purging
    happens, train_idx is just 'everything not in test'."""
    samples = _seq(10, step_minutes=10, label_minutes=5)
    cv = PurgedKFold(n_splits=2, samples=samples)
    folds = list(cv.split())
    train0, test0 = folds[0]
    # Fold 0 test is [0..4]; train should be [5..9] (nothing purged).
    assert sorted(train0.tolist()) == [5, 6, 7, 8, 9]


# ---------- Embargo ----------

def test_embargo_drops_post_test_rows():
    """With n=20, embargo_frac=0.25 → embargo_n=5. After the test fold
    ends at index t, the next 5 training samples are dropped."""
    samples = _seq(20, step_minutes=10, label_minutes=5)
    cv = PurgedKFold(n_splits=4, samples=samples, embargo_frac=0.25)
    folds = list(cv.split())
    # Fold 0: test = [0..4]; embargo zone = [5..9]; training should have NONE of [5..9]
    train0, test0 = folds[0]
    embargo_zone = set(range(5, 10))
    assert embargo_zone.isdisjoint(set(train0.tolist())), (
        f"embargo zone {embargo_zone} leaked into train {train0.tolist()}"
    )


def test_no_embargo_default():
    """embargo_frac=0 → no embargo applied, samples just after test fold
    are still in train (assuming no purge)."""
    samples = _seq(10, step_minutes=10, label_minutes=5)
    cv = PurgedKFold(n_splits=2, samples=samples)  # embargo_frac=0
    train0, test0 = list(cv.split())[0]
    # Sample 5 (right after test fold ends at 4) should be in training.
    assert 5 in train0.tolist()


# ---------- Trade helpers ----------

def test_samples_from_trades_skips_missing_times():
    trades = [
        {"entry_time": "2026-01-01T10:00:00", "exit_time": "2026-01-01T10:30:00"},
        {"entry_time": None, "exit_time": "2026-01-01T11:00:00"},  # skip
        {"exit_time": "2026-01-01T11:30:00"},  # skip
        {"entry_time": "2026-01-01T12:00:00", "exit_time": "2026-01-01T12:15:00"},
    ]
    out = samples_from_trades(trades)
    assert len(out) == 2


def test_count_naive_leakage_zero_when_no_overlap():
    samples = _seq(10, step_minutes=10, label_minutes=5)
    # No labels touch → no leakage even under naive split.
    assert count_naive_leakage(samples, n_splits=2) == 0


def test_count_naive_leakage_detects_overlap():
    """A long-running trade that spans multiple folds creates leakage."""
    base = datetime(2026, 1, 1)
    samples = [
        Sample(base + timedelta(minutes=i * 10),
               base + timedelta(minutes=i * 10 + 5))
        for i in range(10)
    ]
    samples[0] = Sample(base, base + timedelta(hours=2))  # spans all folds
    leaks = count_naive_leakage(samples, n_splits=2)
    assert leaks > 0
