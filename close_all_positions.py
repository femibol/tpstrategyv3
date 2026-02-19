#!/usr/bin/env python3
"""Emergency: Close ALL open Alpaca positions.
Run this on Render or locally where you have network access to Alpaca.

Usage:
    python close_all_positions.py          # dry run (shows what would close)
    python close_all_positions.py --execute # actually closes everything
"""
import sys
import requests
from bot.config import Config

def main():
    execute = "--execute" in sys.argv

    c = Config()
    headers = {
        "APCA-API-KEY-ID": c.alpaca_api_key,
        "APCA-API-SECRET-KEY": c.alpaca_secret_key,
    }
    base = getattr(c, 'alpaca_base_url', 'https://paper-api.alpaca.markets')

    # Get account
    acct = requests.get(f"{base}/v2/account", headers=headers, timeout=10).json()
    print(f"Account equity: ${float(acct['equity']):,.2f}")
    print(f"Cash: ${float(acct['cash']):,.2f}")
    print(f"Buying power: ${float(acct['buying_power']):,.2f}")
    print()

    # Get positions
    resp = requests.get(f"{base}/v2/positions", headers=headers, timeout=10)
    positions = resp.json()
    print(f"Open positions: {len(positions)}")

    total_value = 0
    for p in positions:
        mkt_val = float(p.get("market_value", 0))
        pnl = float(p.get("unrealized_pl", 0))
        total_value += mkt_val
        print(f"  {p['symbol']:6s} | {p['qty']:>6s} shares | ${mkt_val:>10,.2f} | P&L: ${pnl:>+8,.2f}")
    print(f"\nTotal market value: ${total_value:,.2f}")

    if not execute:
        print("\n--- DRY RUN --- Add --execute to actually close all positions")
        return

    print(f"\nClosing ALL {len(positions)} positions...")
    # DELETE /v2/positions closes everything in one call
    resp = requests.delete(
        f"{base}/v2/positions",
        headers=headers,
        params={"cancel_orders": "true"},
        timeout=30,
    )
    if resp.status_code in (200, 204, 207):
        closed = resp.json() if resp.text else []
        print(f"Closed {len(closed)} positions successfully.")
        for order in closed:
            body = order.get("body", {})
            print(f"  {body.get('symbol', '?')}: {body.get('status', '?')}")
    else:
        print(f"ERROR: HTTP {resp.status_code} | {resp.text[:300]}")

    # Verify
    remaining = requests.get(f"{base}/v2/positions", headers=headers, timeout=10).json()
    print(f"\nRemaining positions: {len(remaining)}")
    acct = requests.get(f"{base}/v2/account", headers=headers, timeout=10).json()
    print(f"Cash after close: ${float(acct['cash']):,.2f}")

if __name__ == "__main__":
    main()
