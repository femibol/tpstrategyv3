#!/usr/bin/env python3
"""Reconcile crypto positions against live market prices.

Reads data/positions_state.json, fetches live spot prices from Coinbase
(public API, no auth), and prints real unrealized P&L per crypto position.

Use this when the TradersPost (or any broker) UI shows suspect prices,
e.g. $0 market value due to a stale paper-account quote feed.
"""
from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path


COINBASE_SPOT = "https://api.coinbase.com/v2/prices/{pair}/spot"
COINGECKO_FALLBACK = (
    "https://api.coingecko.com/api/v3/simple/price"
    "?ids={ids}&vs_currencies=usd"
)
SYMBOL_TO_COINGECKO_ID = {
    "BTC": "bitcoin", "ETH": "ethereum", "SOL": "solana",
    "DOGE": "dogecoin", "LTC": "litecoin", "XRP": "ripple",
    "ADA": "cardano", "AVAX": "avalanche-2", "LINK": "chainlink",
    "MATIC": "matic-network", "DOT": "polkadot", "UNI": "uniswap",
    "ATOM": "cosmos", "BCH": "bitcoin-cash", "NEAR": "near",
    "ICP": "internet-computer", "RNDR": "render-token",
    "SUI": "sui", "FIL": "filecoin", "AAVE": "aave",
    "ETC": "ethereum-classic", "INJ": "injective-protocol",
}


def fetch_coinbase(pair: str) -> float | None:
    try:
        with urllib.request.urlopen(COINBASE_SPOT.format(pair=pair), timeout=5) as r:
            data = json.load(r)
        return float(data["data"]["amount"])
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError):
        return None


def fetch_coingecko(symbol: str) -> float | None:
    cg_id = SYMBOL_TO_COINGECKO_ID.get(symbol)
    if not cg_id:
        return None
    try:
        url = COINGECKO_FALLBACK.format(ids=cg_id)
        with urllib.request.urlopen(url, timeout=5) as r:
            data = json.load(r)
        return float(data[cg_id]["usd"])
    except (urllib.error.URLError, KeyError, ValueError, TimeoutError):
        return None


def live_price(symbol_dash_usd: str) -> tuple[float | None, str]:
    """Return (price, source) for a SYMBOL-USD pair."""
    base = symbol_dash_usd.split("-")[0]
    price = fetch_coinbase(symbol_dash_usd)
    if price is not None:
        return price, "coinbase"
    price = fetch_coingecko(base)
    if price is not None:
        return price, "coingecko"
    return None, "none"


def is_crypto(sym: str) -> bool:
    return "-USD" in sym or "/USD" in sym


def main() -> int:
    state_path = Path(__file__).resolve().parent.parent / "data" / "positions_state.json"
    if not state_path.exists():
        print(f"ERROR: {state_path} not found", file=sys.stderr)
        return 1

    with state_path.open() as f:
        positions = json.load(f)

    crypto = {s: p for s, p in positions.items() if is_crypto(s)}
    if not crypto:
        print("No crypto positions open.")
        return 0

    print(f"{'Symbol':10s} {'Qty':>12s} {'Entry':>10s} {'Live':>10s} "
          f"{'BotPx':>10s} {'Cost':>10s} {'MktVal':>10s} {'PnL':>10s} {'PnL%':>7s} Src")
    print("-" * 110)

    total_cost = 0.0
    total_mkt = 0.0
    for sym, pos in sorted(crypto.items()):
        qty = float(pos.get("quantity", 0) or 0)
        entry = float(pos.get("entry_price", 0) or 0)
        bot_px = float(pos.get("current_price", 0) or 0)
        cost = qty * entry

        live, src = live_price(sym)
        if live is None:
            print(f"{sym:10s} {qty:12.5f} {entry:10.4f}     n/a     {bot_px:10.4f} "
                  f"{cost:10.2f}     n/a       n/a    n/a  {src}")
            continue

        mkt = qty * live
        pnl = mkt - cost
        pnl_pct = (pnl / cost * 100) if cost else 0
        total_cost += cost
        total_mkt += mkt
        print(f"{sym:10s} {qty:12.5f} {entry:10.4f} {live:10.4f} {bot_px:10.4f} "
              f"{cost:10.2f} {mkt:10.2f} {pnl:+10.2f} {pnl_pct:+6.2f}% {src}")

    print("-" * 110)
    total_pnl = total_mkt - total_cost
    total_pct = (total_pnl / total_cost * 100) if total_cost else 0
    print(f"{'TOTAL':10s} {'':12s} {'':10s} {'':10s} {'':10s} "
          f"{total_cost:10.2f} {total_mkt:10.2f} {total_pnl:+10.2f} {total_pct:+6.2f}%")
    return 0


if __name__ == "__main__":
    sys.exit(main())
