#!/usr/bin/env python3
"""Fetch trade history from Alpaca broker API (where TradersPost executes orders)."""
import requests
import json
import os
import sys
from datetime import datetime, timedelta
from pathlib import Path

# Load .env
from dotenv import load_dotenv
load_dotenv()

key = os.getenv("ALPACA_API_KEY")
secret = os.getenv("ALPACA_SECRET_KEY")
base = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

headers = {
    "APCA-API-KEY-ID": key,
    "APCA-API-SECRET-KEY": secret,
}

data_dir = Path(__file__).parent

def get_account():
    r = requests.get(f"{base}/v2/account", headers=headers)
    if not r.ok:
        print(f"Account error: {r.status_code} {r.text}")
        return None
    return r.json()

def get_positions():
    r = requests.get(f"{base}/v2/positions", headers=headers)
    if not r.ok:
        print(f"Positions error: {r.status_code} {r.text}")
        return []
    return r.json()

def get_closed_orders(limit=500):
    r = requests.get(f"{base}/v2/orders", headers=headers, params={
        "status": "closed", "limit": limit, "direction": "desc"
    })
    if not r.ok:
        print(f"Orders error: {r.status_code} {r.text}")
        return []
    return r.json()

def get_activities(limit=500):
    """Get account activities (fills) - most accurate for P&L."""
    r = requests.get(f"{base}/v2/account/activities/FILL", headers=headers, params={
        "direction": "desc", "page_size": limit
    })
    if not r.ok:
        print(f"Activities error: {r.status_code} {r.text}")
        return []
    return r.json()

def main():
    print("=" * 70)
    print("  ALPACA BROKER TRADE HISTORY")
    print("=" * 70)

    # Account
    acct = get_account()
    if acct:
        print(f"\n  Account Status: {acct.get('status')}")
        print(f"  Equity:         ${float(acct.get('equity', 0)):>12,.2f}")
        print(f"  Cash:           ${float(acct.get('cash', 0)):>12,.2f}")
        print(f"  Portfolio:      ${float(acct.get('portfolio_value', 0)):>12,.2f}")
        print(f"  Buying Power:   ${float(acct.get('buying_power', 0)):>12,.2f}")
        print(f"  Day Trade Count: {acct.get('daytrade_count', 'N/A')}")
    else:
        print("\n  Could not connect to Alpaca.")
        return

    # Open positions
    positions = get_positions()
    print(f"\n  --- Open Positions ({len(positions)}) ---")
    total_unrealized = 0
    for p in positions:
        sym = p["symbol"]
        qty = int(float(p["qty"]))
        entry = float(p["avg_entry_price"])
        current = float(p["current_price"])
        pnl = float(p["unrealized_pl"])
        pnl_pct = float(p["unrealized_plpc"]) * 100
        mkt_val = float(p["market_value"])
        total_unrealized += pnl
        print(f"    {sym:6s} | {qty:>4d} @ ${entry:>8.2f} | Now ${current:>8.2f} | P&L ${pnl:>8.2f} ({pnl_pct:>+.1f}%)")
    if positions:
        print(f"    {'':6s}   Total Unrealized P&L: ${total_unrealized:>8.2f}")

    # Closed orders
    orders = get_closed_orders()
    filled = [o for o in orders if o.get("filled_avg_price")]
    print(f"\n  --- Closed Orders ({len(filled)} filled) ---")

    total_realized = 0
    buys = {}  # symbol -> list of (price, qty, time)

    for o in reversed(filled):  # chronological order
        sym = o["symbol"]
        side = o["side"]
        qty = int(float(o.get("filled_qty", o["qty"])))
        price = float(o["filled_avg_price"])
        created = o["created_at"][:19]
        status = o["status"]

        marker = "BUY " if side == "buy" else "SELL"
        print(f"    {created} | {marker} {sym:6s} x{qty:>4d} @ ${price:>8.2f} | {status}")

    # Get fills for P&L
    fills = get_activities()
    print(f"\n  --- Trade Fills ({len(fills)}) ---")

    # Match fills into round-trip trades
    open_buys = {}  # symbol -> [(price, qty, time)]
    completed = []

    for f in reversed(fills):  # chronological
        sym = f.get("symbol", "")
        side = f.get("side", "")
        qty = int(float(f.get("qty", 0)))
        price = float(f.get("price", 0))
        ts = f.get("transaction_time", f.get("timestamp", ""))[:19]

        if side == "buy":
            if sym not in open_buys:
                open_buys[sym] = []
            open_buys[sym].append({"price": price, "qty": qty, "time": ts})
        elif side in ("sell", "sell_short"):
            remaining = qty
            while remaining > 0 and open_buys.get(sym):
                entry = open_buys[sym][0]
                match_qty = min(remaining, entry["qty"])
                pnl = (price - entry["price"]) * match_qty
                completed.append({
                    "symbol": sym,
                    "entry_price": entry["price"],
                    "exit_price": price,
                    "qty": match_qty,
                    "pnl": pnl,
                    "pnl_pct": ((price - entry["price"]) / entry["price"]) * 100,
                    "entry_time": entry["time"],
                    "exit_time": ts,
                })
                entry["qty"] -= match_qty
                remaining -= match_qty
                if entry["qty"] <= 0:
                    open_buys[sym].pop(0)

    if completed:
        print(f"\n  --- Matched Round-Trip Trades ({len(completed)}) ---")
        wins = [t for t in completed if t["pnl"] > 0]
        losses = [t for t in completed if t["pnl"] < 0]
        flat = [t for t in completed if t["pnl"] == 0]
        total_pnl = sum(t["pnl"] for t in completed)
        gross_profit = sum(t["pnl"] for t in wins) if wins else 0
        gross_loss = sum(t["pnl"] for t in losses) if losses else 0

        print(f"    Winners: {len(wins)} | Losers: {len(losses)} | Flat: {len(flat)}")
        wr = (len(wins) / len(completed) * 100) if completed else 0
        print(f"    Win Rate: {wr:.1f}%")
        print(f"    Total P&L: ${total_pnl:>+.2f}")
        print(f"    Gross Profit: ${gross_profit:>+.2f}")
        print(f"    Gross Loss:   ${gross_loss:>+.2f}")
        if gross_loss != 0:
            print(f"    Profit Factor: {abs(gross_profit/gross_loss):.2f}")
        if wins:
            avg_win = gross_profit / len(wins)
            print(f"    Avg Win:  ${avg_win:>+.2f} ({sum(t['pnl_pct'] for t in wins)/len(wins):>+.2f}%)")
        if losses:
            avg_loss = gross_loss / len(losses)
            print(f"    Avg Loss: ${avg_loss:>+.2f} ({sum(t['pnl_pct'] for t in losses)/len(losses):>+.2f}%)")

        print(f"\n    --- All Trades ---")
        for i, t in enumerate(completed, 1):
            tag = "W" if t["pnl"] > 0 else ("L" if t["pnl"] < 0 else "-")
            print(
                f"    {i:>3d}. [{tag}] {t['symbol']:6s} | "
                f"${t['entry_price']:>8.2f} -> ${t['exit_price']:>8.2f} x{t['qty']:>4d} | "
                f"P&L ${t['pnl']:>+8.2f} ({t['pnl_pct']:>+.2f}%)"
            )

        # Save to JSON
        output = data_dir / "alpaca_trade_history.json"
        with open(output, "w") as fp:
            json.dump({
                "account": {
                    "equity": float(acct.get("equity", 0)),
                    "cash": float(acct.get("cash", 0)),
                    "portfolio_value": float(acct.get("portfolio_value", 0)),
                },
                "open_positions": [{
                    "symbol": p["symbol"],
                    "qty": int(float(p["qty"])),
                    "entry_price": float(p["avg_entry_price"]),
                    "current_price": float(p["current_price"]),
                    "unrealized_pnl": float(p["unrealized_pl"]),
                } for p in positions],
                "completed_trades": completed,
                "stats": {
                    "total_trades": len(completed),
                    "wins": len(wins),
                    "losses": len(losses),
                    "flat": len(flat),
                    "win_rate": wr,
                    "total_pnl": total_pnl,
                    "gross_profit": gross_profit,
                    "gross_loss": gross_loss,
                },
                "fetched_at": datetime.utcnow().isoformat(),
            }, fp, indent=2, default=str)
        print(f"\n    Saved to {output}")
    else:
        print("\n  No matched round-trip trades found.")

    print("\n" + "=" * 70)

if __name__ == "__main__":
    main()
