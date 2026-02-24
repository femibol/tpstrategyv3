#!/usr/bin/env python3
"""
Analyze TradersPost webhook history.
Reads tp_webhook_history.json, matches buys to exits, calculates P&L.

Usage:
    python data/analyze_webhook_history.py
    python data/analyze_webhook_history.py --verbose
"""
import json
import sys
from collections import defaultdict
from pathlib import Path


def load_data(filepath):
    with open(filepath) as f:
        return json.load(f)


def match_trades(signals):
    """Match buy entries to their corresponding exits (FIFO per ticker)."""
    # Sort by timestamp (already in order from webhook logs)
    open_positions = defaultdict(list)  # ticker -> [(entry_signal, ...)]
    completed_trades = []

    for sig in signals:
        ticker = sig["ticker"]
        action = sig["action"]
        qty = sig.get("quantity", 0)
        price = sig.get("price", 0)

        if action == "buy":
            open_positions[ticker].append(sig)
        elif action == "exit":
            # Match with oldest open position for this ticker (FIFO)
            if open_positions[ticker]:
                entry = open_positions[ticker].pop(0)
                entry_price = entry.get("price", 0)
                entry_qty = entry.get("quantity", 0)
                exit_price = price
                exit_qty = qty

                # Use min quantity for matching
                matched_qty = min(entry_qty, exit_qty) if entry_qty and exit_qty else exit_qty

                pnl = (exit_price - entry_price) * matched_qty if entry_price and exit_price else 0
                pnl_pct = ((exit_price / entry_price) - 1) * 100 if entry_price else 0

                completed_trades.append({
                    "ticker": ticker,
                    "entry_price": round(entry_price, 4),
                    "exit_price": round(exit_price, 4),
                    "quantity": matched_qty,
                    "pnl": round(pnl, 2),
                    "pnl_pct": round(pnl_pct, 4),
                    "entry_time": entry.get("timestamp", ""),
                    "exit_time": sig.get("timestamp", ""),
                    "stop_loss": entry.get("stopLoss", {}).get("stopPrice", 0),
                    "take_profit": entry.get("takeProfit", {}).get("limitPrice", 0),
                })

    return completed_trades, open_positions


def print_summary(trades, open_positions, verbose=False):
    if not trades:
        print("No completed trades found.")
        return

    total_pnl = sum(t["pnl"] for t in trades)
    winners = [t for t in trades if t["pnl"] > 0]
    losers = [t for t in trades if t["pnl"] < 0]
    flat = [t for t in trades if t["pnl"] == 0]

    win_rate = len(winners) / len(trades) * 100 if trades else 0
    avg_win = sum(t["pnl"] for t in winners) / len(winners) if winners else 0
    avg_loss = sum(t["pnl"] for t in losers) / len(losers) if losers else 0
    avg_win_pct = sum(t["pnl_pct"] for t in winners) / len(winners) if winners else 0
    avg_loss_pct = sum(t["pnl_pct"] for t in losers) / len(losers) if losers else 0

    total_invested = sum(t["entry_price"] * t["quantity"] for t in trades)
    total_return_pct = (total_pnl / total_invested * 100) if total_invested else 0

    print("=" * 70)
    print("  TRADERSPOST WEBHOOK TRADE ANALYSIS")
    print("=" * 70)
    print(f"\n  Total Completed Trades: {len(trades)}")
    print(f"  Winners: {len(winners)}  |  Losers: {len(losers)}  |  Flat: {len(flat)}")
    print(f"  Win Rate: {win_rate:.1f}%")
    print(f"\n  Total P&L: ${total_pnl:,.2f}")
    print(f"  Total Capital Deployed: ${total_invested:,.2f}")
    print(f"  Return on Capital: {total_return_pct:.2f}%")
    print(f"\n  Avg Win:  ${avg_win:,.2f} ({avg_win_pct:.2f}%)")
    print(f"  Avg Loss: ${avg_loss:,.2f} ({avg_loss_pct:.2f}%)")

    if avg_loss != 0:
        profit_factor = abs(sum(t["pnl"] for t in winners) / sum(t["pnl"] for t in losers))
        print(f"  Profit Factor: {profit_factor:.2f}")

    # Best and worst trades
    sorted_by_pnl = sorted(trades, key=lambda t: t["pnl"], reverse=True)
    print(f"\n  --- Top 5 Winners ---")
    for t in sorted_by_pnl[:5]:
        print(f"    {t['ticker']:6s} | ${t['pnl']:>8.2f} ({t['pnl_pct']:>6.2f}%) | "
              f"Buy ${t['entry_price']:.2f} -> Sell ${t['exit_price']:.2f} x {t['quantity']}")

    print(f"\n  --- Top 5 Losers ---")
    for t in sorted_by_pnl[-5:]:
        print(f"    {t['ticker']:6s} | ${t['pnl']:>8.2f} ({t['pnl_pct']:>6.2f}%) | "
              f"Buy ${t['entry_price']:.2f} -> Sell ${t['exit_price']:.2f} x {t['quantity']}")

    # Performance by ticker
    ticker_pnl = defaultdict(lambda: {"pnl": 0, "trades": 0, "wins": 0, "invested": 0})
    for t in trades:
        tk = t["ticker"]
        ticker_pnl[tk]["pnl"] += t["pnl"]
        ticker_pnl[tk]["trades"] += 1
        ticker_pnl[tk]["invested"] += t["entry_price"] * t["quantity"]
        if t["pnl"] > 0:
            ticker_pnl[tk]["wins"] += 1

    print(f"\n  --- P&L by Ticker (sorted by total P&L) ---")
    sorted_tickers = sorted(ticker_pnl.items(), key=lambda x: x[1]["pnl"], reverse=True)
    for ticker, stats in sorted_tickers:
        wr = stats["wins"] / stats["trades"] * 100 if stats["trades"] else 0
        ret = stats["pnl"] / stats["invested"] * 100 if stats["invested"] else 0
        print(f"    {ticker:6s} | ${stats['pnl']:>8.2f} | {stats['trades']:>3d} trades | "
              f"WR {wr:>5.1f}% | Return {ret:>6.2f}%")

    # Stop loss analysis
    print(f"\n  --- Stop Loss Analysis ---")
    trades_with_sl = [t for t in trades if t.get("stop_loss")]
    if trades_with_sl:
        hit_sl = [t for t in trades_with_sl if t["exit_price"] <= t["stop_loss"] and t["pnl"] < 0]
        hit_tp = [t for t in trades_with_sl if t.get("take_profit") and t["exit_price"] >= t["take_profit"]]
        neither = [t for t in trades_with_sl if t not in hit_sl and t not in hit_tp]
        print(f"    Trades with SL: {len(trades_with_sl)}")
        print(f"    Hit Stop Loss: {len(hit_sl)} ({len(hit_sl)/len(trades_with_sl)*100:.1f}%)")
        print(f"    Hit Take Profit: {len(hit_tp)} ({len(hit_tp)/len(trades_with_sl)*100:.1f}%)")
        print(f"    Exited between SL/TP: {len(neither)} ({len(neither)/len(trades_with_sl)*100:.1f}%)")

    # Risk/Reward analysis
    print(f"\n  --- Risk/Reward Analysis ---")
    rr_trades = [t for t in trades if t.get("stop_loss") and t.get("take_profit") and t["entry_price"]]
    if rr_trades:
        rr_ratios = []
        for t in rr_trades:
            risk = abs(t["entry_price"] - t["stop_loss"])
            reward = abs(t["take_profit"] - t["entry_price"])
            if risk > 0:
                rr_ratios.append(reward / risk)
        if rr_ratios:
            avg_rr = sum(rr_ratios) / len(rr_ratios)
            print(f"    Avg Target R:R = {avg_rr:.2f}:1")
            tight_stops = [r for r in rr_ratios if r > 3]
            print(f"    Trades with R:R > 3:1 = {len(tight_stops)} ({len(tight_stops)/len(rr_ratios)*100:.1f}%)")
            low_rr = [r for r in rr_ratios if r < 1]
            print(f"    Trades with R:R < 1:1 = {len(low_rr)} ({len(low_rr)/len(rr_ratios)*100:.1f}%)")

    # Open positions still held
    open_count = sum(len(v) for v in open_positions.values())
    if open_count:
        print(f"\n  --- Still Open ({open_count} positions) ---")
        for ticker, entries in open_positions.items():
            if entries:
                for e in entries:
                    print(f"    {ticker:6s} | Qty {e.get('quantity', '?'):>5} @ ${e.get('price', 0):.2f}")

    if verbose:
        print(f"\n  --- All Trades (chronological) ---")
        for i, t in enumerate(trades, 1):
            marker = "W" if t["pnl"] > 0 else ("L" if t["pnl"] < 0 else "-")
            print(f"    {i:>3}. [{marker}] {t['ticker']:6s} | ${t['pnl']:>8.2f} ({t['pnl_pct']:>6.2f}%) | "
                  f"${t['entry_price']:.2f} -> ${t['exit_price']:.2f} x {t['quantity']}")

    print("\n" + "=" * 70)


def main():
    data_dir = Path(__file__).parent
    filepath = data_dir / "tp_webhook_history.json"

    if not filepath.exists():
        print(f"Error: {filepath} not found. Run parse_webhook_logs.py first or provide data.")
        sys.exit(1)

    verbose = "--verbose" in sys.argv or "-v" in sys.argv

    signals = load_data(filepath)
    print(f"Loaded {len(signals)} webhook signals")

    trades, open_positions = match_trades(signals)
    print_summary(trades, open_positions, verbose=verbose)


if __name__ == "__main__":
    main()
