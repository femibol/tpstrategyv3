#!/usr/bin/env python3
"""Pull trade history from the live dashboard for offline review.

Used by Claude Code sessions that can't reach the VPS filesystem directly.
Reads TPSTRAT_DASHBOARD_URL + TPSTRAT_DASHBOARD_KEY from the environment
(see docs in `code.claude.com` environment settings), hits /api/trades, and
writes the filtered slice to .review/trades_<bucket>.json.

Usage:
    python3 scripts/fetch_review_data.py              # last 500 trades, all assets
    python3 scripts/fetch_review_data.py --crypto     # crypto-only
    python3 scripts/fetch_review_data.py --equity     # equity-only
    python3 scripts/fetch_review_data.py --since 2026-05-20 --limit 1000

Writes to .review/ which is local-only; nothing here gets committed.
"""

import argparse
import json
import os
import sys
import urllib.parse
import urllib.request
from base64 import b64encode
from pathlib import Path


def fetch(url: str, user: str, key: str, params: dict) -> list:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    full = f"{url.rstrip('/')}/api/trades?{qs}"
    auth = b64encode(f"{user}:{key}".encode()).decode()
    req = urllib.request.Request(full, headers={"Authorization": f"Basic {auth}"})
    with urllib.request.urlopen(req, timeout=20) as resp:
        return json.loads(resp.read())


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--since", help="ISO date, e.g. 2026-05-20")
    parser.add_argument("--until", help="ISO date, e.g. 2026-05-25")
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--strategy")
    parser.add_argument("--symbol")
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--crypto", action="store_true", help="filter symbols ending in -USD")
    group.add_argument("--equity", action="store_true", help="filter symbols NOT ending in -USD")
    args = parser.parse_args()

    url = os.environ.get("TPSTRAT_DASHBOARD_URL")
    key = os.environ.get("TPSTRAT_DASHBOARD_KEY")
    user = os.environ.get("TPSTRAT_DASHBOARD_USER", "admin")
    if not url or not key:
        print("ERROR: set TPSTRAT_DASHBOARD_URL and TPSTRAT_DASHBOARD_KEY", file=sys.stderr)
        return 2

    trades = fetch(url, user, key, {
        "start": args.since,
        "end": args.until,
        "limit": args.limit,
        "strategy": args.strategy,
        "symbol": args.symbol,
    })

    if args.crypto:
        trades = [t for t in trades if str(t.get("symbol", "")).endswith("-USD")]
        bucket = "crypto"
    elif args.equity:
        trades = [t for t in trades if not str(t.get("symbol", "")).endswith("-USD")]
        bucket = "equity"
    else:
        bucket = "all"

    out_dir = Path(".review")
    out_dir.mkdir(exist_ok=True)
    out = out_dir / f"trades_{bucket}.json"
    out.write_text(json.dumps(trades, indent=2))

    wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
    pnl = sum((t.get("pnl") or 0) for t in trades)
    print(f"wrote {out} | {len(trades)} trades | {wins}W ({wins/len(trades)*100:.1f}% WR) | net ${pnl:+.2f}"
          if trades else f"wrote {out} | 0 trades")
    return 0


if __name__ == "__main__":
    sys.exit(main())
