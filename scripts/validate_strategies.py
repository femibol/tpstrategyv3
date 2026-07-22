#!/usr/bin/env python3
"""Print the strategy validation / robustness report.

Answers "real edge or lucky streak?" per strategy against the pre-committed
go-live bar (see bot/learning/strategy_validator). Reads a trade-history JSON
(default data/trade_history.json).

Usage:
    scripts/validate_strategies.py [path/to/trade_history.json]
    scripts/validate_strategies.py --min-trades 100 --pf-min 1.2 --top-pct 0.05
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from bot.learning.strategy_validator import build_report, format_report  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("path", nargs="?", default="data/trade_history.json",
                    help="trade history JSON (default: data/trade_history.json)")
    ap.add_argument("--min-trades", type=int)
    ap.add_argument("--pf-min", type=float)
    ap.add_argument("--top-pct", type=float)
    ap.add_argument("--json", action="store_true", help="emit raw report JSON")
    args = ap.parse_args()

    p = Path(args.path)
    if not p.exists():
        print(f"trade history not found: {p}", file=sys.stderr)
        return 1
    trades = json.loads(p.read_text())

    bar = {}
    if args.min_trades is not None:
        bar["min_trades"] = args.min_trades
    if args.pf_min is not None:
        bar["pf_min"] = args.pf_min
    if args.top_pct is not None:
        bar["top_pct"] = args.top_pct

    report = build_report(trades, bar or None)
    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print(format_report(report))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
