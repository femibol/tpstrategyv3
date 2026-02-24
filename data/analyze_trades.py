#!/usr/bin/env python3
"""
Analyze TradersPost webhook trade history.

Reads the parsed JSON file (tp_webhook_history.json) and:
1. Matches buy entries to their corresponding exit signals (same ticker)
2. Calculates P&L per round-trip trade
3. Shows overall statistics: win rate, avg win, avg loss, profit factor
4. Shows performance breakdown by ticker
5. Shows best/worst trades
6. Optionally exports matched trades to CSV

Usage:
    python analyze_trades.py [json_file]
    python analyze_trades.py tp_webhook_history.json
    python analyze_trades.py --csv    # also export to CSV

If json_file is omitted, defaults to tp_webhook_history.json in the same
directory as this script.
"""

import csv
import json
import sys
from collections import defaultdict
from datetime import datetime
from pathlib import Path

# Default paths
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_INPUT = SCRIPT_DIR / "tp_webhook_history.json"
DEFAULT_CSV_OUTPUT = SCRIPT_DIR / "matched_trades.csv"


def load_records(path: str) -> list[dict]:
    """Load parsed webhook records from JSON file."""
    p = Path(path)
    if not p.exists():
        print(f"ERROR: File not found: {p}")
        print(f"Run parse_webhook_logs.py first to create it.")
        sys.exit(1)

    with open(p, "r") as f:
        records = json.load(f)

    print(f"Loaded {len(records)} records from {p}")
    return records


def classify_action(record: dict) -> str:
    """
    Classify a record as 'entry' or 'exit'.

    TradersPost actions:
        buy     -> entry (open long)
        exit    -> exit (close position)
        sell    -> could be exit or short entry; treat as exit if no 'bullish' sentiment
    """
    action = record.get("action", "").lower()
    sentiment = record.get("sentiment", "").lower()

    if action == "buy":
        return "entry"
    elif action == "exit":
        return "exit"
    elif action == "sell":
        # In a bullish-only system, sell is always an exit
        # If sentiment is bearish, it is definitely an exit
        if sentiment == "bullish":
            return "entry_short"  # rare, but handle it
        return "exit"
    elif action in ("close", "cover"):
        return "exit"
    else:
        return "unknown"


def match_trades(records: list[dict]) -> list[dict]:
    """
    Match buy entries to their corresponding exits using FIFO per ticker.

    Returns a list of matched round-trip trades with P&L.
    """
    # Separate entries and exits, sorted by timestamp (or line order)
    entries_by_ticker = defaultdict(list)  # {ticker: [record, ...]}
    exits_by_ticker = defaultdict(list)

    for r in records:
        ticker = r.get("ticker", "")
        if not ticker:
            continue

        classification = classify_action(r)
        if classification == "entry":
            entries_by_ticker[ticker].append(r)
        elif classification == "exit":
            exits_by_ticker[ticker].append(r)
        # Ignore unknown/short entries for now

    matched = []
    unmatched_entries = []
    unmatched_exits = []

    all_tickers = set(list(entries_by_ticker.keys()) + list(exits_by_ticker.keys()))

    for ticker in sorted(all_tickers):
        entries = list(entries_by_ticker.get(ticker, []))
        exits = list(exits_by_ticker.get(ticker, []))

        # FIFO matching: match each exit to the earliest unmatched entry
        entry_idx = 0
        for ex in exits:
            if entry_idx < len(entries):
                entry = entries[entry_idx]

                # Calculate P&L
                entry_price = float(entry.get("price", 0))
                exit_price = float(ex.get("price", 0))
                quantity = int(entry.get("quantity", 0) or ex.get("quantity", 0))

                if entry_price > 0 and exit_price > 0:
                    pnl_per_share = exit_price - entry_price
                    pnl_total = pnl_per_share * quantity if quantity else pnl_per_share
                    pnl_pct = (pnl_per_share / entry_price) * 100
                else:
                    pnl_per_share = 0
                    pnl_total = 0
                    pnl_pct = 0

                trade = {
                    "ticker": ticker,
                    "entry_time": entry.get("timestamp", ""),
                    "exit_time": ex.get("timestamp", ""),
                    "entry_price": entry_price,
                    "exit_price": exit_price,
                    "quantity": quantity,
                    "pnl_per_share": round(pnl_per_share, 4),
                    "pnl_total": round(pnl_total, 2),
                    "pnl_pct": round(pnl_pct, 2),
                    "is_win": pnl_total > 0,
                    "entry_log_id": entry.get("log_id", ""),
                    "exit_log_id": ex.get("log_id", ""),
                    "entry_sentiment": entry.get("sentiment", ""),
                    "stop_loss": entry.get("stop_loss", 0),
                    "take_profit": entry.get("take_profit", 0),
                }

                # Determine exit reason by comparing to stop/take-profit levels
                sl = float(entry.get("stop_loss", 0) or 0)
                tp = float(entry.get("take_profit", 0) or 0)
                if sl and exit_price <= sl:
                    trade["exit_reason"] = "stop_loss"
                elif tp and exit_price >= tp:
                    trade["exit_reason"] = "take_profit"
                else:
                    trade["exit_reason"] = "signal_exit"

                matched.append(trade)
                entry_idx += 1
            else:
                unmatched_exits.append(ex)

        # Remaining unmatched entries (still open or no exit found)
        for i in range(entry_idx, len(entries)):
            unmatched_entries.append(entries[i])

    return matched, unmatched_entries, unmatched_exits


def calculate_stats(trades: list[dict]) -> dict:
    """Calculate overall trading statistics."""
    if not trades:
        return {}

    wins = [t for t in trades if t["pnl_total"] > 0]
    losses = [t for t in trades if t["pnl_total"] < 0]
    breakeven = [t for t in trades if t["pnl_total"] == 0]

    total_pnl = sum(t["pnl_total"] for t in trades)
    gross_profit = sum(t["pnl_total"] for t in wins) if wins else 0
    gross_loss = sum(t["pnl_total"] for t in losses) if losses else 0

    avg_win = gross_profit / len(wins) if wins else 0
    avg_loss = gross_loss / len(losses) if losses else 0

    # Profit factor = gross profit / abs(gross loss)
    profit_factor = gross_profit / abs(gross_loss) if gross_loss != 0 else float("inf")

    # Expectancy = (win_rate * avg_win) + (loss_rate * avg_loss)
    win_rate = len(wins) / len(trades) * 100 if trades else 0
    loss_rate = len(losses) / len(trades) * 100 if trades else 0
    expectancy = (win_rate / 100 * avg_win) + (loss_rate / 100 * avg_loss)

    # Max drawdown (running P&L)
    running = 0
    peak = 0
    max_drawdown = 0
    for t in trades:
        running += t["pnl_total"]
        if running > peak:
            peak = running
        dd = peak - running
        if dd > max_drawdown:
            max_drawdown = dd

    # Consecutive wins/losses
    max_consec_wins = 0
    max_consec_losses = 0
    current_streak = 0
    for t in trades:
        if t["pnl_total"] > 0:
            current_streak = max(1, current_streak + 1) if current_streak > 0 else 1
            max_consec_wins = max(max_consec_wins, current_streak)
        elif t["pnl_total"] < 0:
            current_streak = min(-1, current_streak - 1) if current_streak < 0 else -1
            max_consec_losses = max(max_consec_losses, abs(current_streak))
        else:
            current_streak = 0

    stats = {
        "total_trades": len(trades),
        "wins": len(wins),
        "losses": len(losses),
        "breakeven": len(breakeven),
        "win_rate": round(win_rate, 1),
        "total_pnl": round(total_pnl, 2),
        "gross_profit": round(gross_profit, 2),
        "gross_loss": round(gross_loss, 2),
        "avg_win": round(avg_win, 2),
        "avg_loss": round(avg_loss, 2),
        "avg_trade": round(total_pnl / len(trades), 2) if trades else 0,
        "profit_factor": round(profit_factor, 2) if profit_factor != float("inf") else "inf",
        "expectancy": round(expectancy, 2),
        "max_drawdown": round(max_drawdown, 2),
        "max_consecutive_wins": max_consec_wins,
        "max_consecutive_losses": max_consec_losses,
        "largest_win": round(max(t["pnl_total"] for t in trades), 2) if trades else 0,
        "largest_loss": round(min(t["pnl_total"] for t in trades), 2) if trades else 0,
    }

    return stats


def performance_by_ticker(trades: list[dict]) -> dict:
    """Calculate per-ticker performance metrics."""
    by_ticker = defaultdict(list)
    for t in trades:
        by_ticker[t["ticker"]].append(t)

    results = {}
    for ticker, ticker_trades in sorted(by_ticker.items()):
        wins = [t for t in ticker_trades if t["pnl_total"] > 0]
        losses = [t for t in ticker_trades if t["pnl_total"] < 0]
        total_pnl = sum(t["pnl_total"] for t in ticker_trades)

        results[ticker] = {
            "trades": len(ticker_trades),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(ticker_trades) * 100, 1) if ticker_trades else 0,
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(ticker_trades), 2) if ticker_trades else 0,
            "best_trade": round(max(t["pnl_total"] for t in ticker_trades), 2),
            "worst_trade": round(min(t["pnl_total"] for t in ticker_trades), 2),
        }

    return results


def performance_by_exit_reason(trades: list[dict]) -> dict:
    """Analyze performance segmented by exit reason."""
    by_reason = defaultdict(list)
    for t in trades:
        reason = t.get("exit_reason", "unknown")
        by_reason[reason].append(t)

    results = {}
    for reason, reason_trades in sorted(by_reason.items()):
        wins = [t for t in reason_trades if t["pnl_total"] > 0]
        total_pnl = sum(t["pnl_total"] for t in reason_trades)
        results[reason] = {
            "trades": len(reason_trades),
            "wins": len(wins),
            "win_rate": round(len(wins) / len(reason_trades) * 100, 1),
            "total_pnl": round(total_pnl, 2),
            "avg_pnl": round(total_pnl / len(reason_trades), 2),
        }

    return results


def print_report(stats: dict, ticker_perf: dict, exit_perf: dict,
                 matched: list, unmatched_entries: list, unmatched_exits: list) -> None:
    """Print a comprehensive trade analysis report."""
    W = 60  # report width

    print(f"\n{'='*W}")
    print(f"{'TRADERSPOST TRADE ANALYSIS':^{W}}")
    print(f"{'='*W}")

    if not stats:
        print("No matched trades found. Nothing to analyze.")
        return

    # Overall stats
    print(f"\n--- OVERALL PERFORMANCE ---")
    print(f"  Total round-trip trades:  {stats['total_trades']}")
    print(f"  Wins:                     {stats['wins']}")
    print(f"  Losses:                   {stats['losses']}")
    print(f"  Breakeven:                {stats['breakeven']}")
    print(f"  Win Rate:                 {stats['win_rate']}%")
    print()
    pnl_sign = "+" if stats['total_pnl'] >= 0 else ""
    print(f"  Total P&L:               {pnl_sign}${stats['total_pnl']:,.2f}")
    print(f"  Gross Profit:            +${stats['gross_profit']:,.2f}")
    print(f"  Gross Loss:              -${abs(stats['gross_loss']):,.2f}")
    print(f"  Avg Trade:               ${stats['avg_trade']:,.2f}")
    print(f"  Avg Win:                 +${stats['avg_win']:,.2f}")
    print(f"  Avg Loss:                -${abs(stats['avg_loss']):,.2f}")
    print(f"  Profit Factor:           {stats['profit_factor']}")
    print(f"  Expectancy per trade:    ${stats['expectancy']:,.2f}")
    print()
    print(f"  Max Drawdown:            ${stats['max_drawdown']:,.2f}")
    print(f"  Max Consecutive Wins:    {stats['max_consecutive_wins']}")
    print(f"  Max Consecutive Losses:  {stats['max_consecutive_losses']}")
    print(f"  Largest Win:             +${stats['largest_win']:,.2f}")
    print(f"  Largest Loss:            -${abs(stats['largest_loss']):,.2f}")

    # Best and worst trades
    if matched:
        sorted_by_pnl = sorted(matched, key=lambda t: t["pnl_total"], reverse=True)

        print(f"\n--- TOP 5 BEST TRADES ---")
        for t in sorted_by_pnl[:5]:
            print(f"  {t['ticker']:8s}  "
                  f"${t['entry_price']:.2f} -> ${t['exit_price']:.2f}  "
                  f"qty={t['quantity']}  "
                  f"P&L: +${t['pnl_total']:,.2f} ({t['pnl_pct']:+.1f}%)  "
                  f"[{t['exit_reason']}]")

        print(f"\n--- TOP 5 WORST TRADES ---")
        for t in sorted_by_pnl[-5:]:
            sign = "+" if t['pnl_total'] >= 0 else ""
            print(f"  {t['ticker']:8s}  "
                  f"${t['entry_price']:.2f} -> ${t['exit_price']:.2f}  "
                  f"qty={t['quantity']}  "
                  f"P&L: {sign}${t['pnl_total']:,.2f} ({t['pnl_pct']:+.1f}%)  "
                  f"[{t['exit_reason']}]")

    # Performance by ticker
    if ticker_perf:
        print(f"\n--- PERFORMANCE BY TICKER ---")
        print(f"  {'Ticker':8s} {'Trades':>6s} {'Wins':>5s} {'WR%':>6s} "
              f"{'Total P&L':>12s} {'Avg P&L':>10s} {'Best':>10s} {'Worst':>10s}")
        print(f"  {'-'*8} {'-'*6} {'-'*5} {'-'*6} {'-'*12} {'-'*10} {'-'*10} {'-'*10}")

        # Sort by total P&L descending
        sorted_tickers = sorted(ticker_perf.items(), key=lambda x: x[1]["total_pnl"], reverse=True)
        for ticker, perf in sorted_tickers:
            pnl_str = f"${perf['total_pnl']:,.2f}"
            avg_str = f"${perf['avg_pnl']:,.2f}"
            best_str = f"${perf['best_trade']:,.2f}"
            worst_str = f"${perf['worst_trade']:,.2f}"
            print(f"  {ticker:8s} {perf['trades']:6d} {perf['wins']:5d} "
                  f"{perf['win_rate']:5.1f}% {pnl_str:>12s} {avg_str:>10s} "
                  f"{best_str:>10s} {worst_str:>10s}")

    # Performance by exit reason
    if exit_perf:
        print(f"\n--- PERFORMANCE BY EXIT REASON ---")
        print(f"  {'Reason':15s} {'Trades':>6s} {'Wins':>5s} {'WR%':>6s} "
              f"{'Total P&L':>12s} {'Avg P&L':>10s}")
        print(f"  {'-'*15} {'-'*6} {'-'*5} {'-'*6} {'-'*12} {'-'*10}")
        for reason, perf in sorted(exit_perf.items(), key=lambda x: x[1]["total_pnl"], reverse=True):
            pnl_str = f"${perf['total_pnl']:,.2f}"
            avg_str = f"${perf['avg_pnl']:,.2f}"
            print(f"  {reason:15s} {perf['trades']:6d} {perf['wins']:5d} "
                  f"{perf['win_rate']:5.1f}% {pnl_str:>12s} {avg_str:>10s}")

    # Unmatched signals
    if unmatched_entries or unmatched_exits:
        print(f"\n--- UNMATCHED SIGNALS ---")
        if unmatched_entries:
            print(f"  Open positions (entry with no exit): {len(unmatched_entries)}")
            for e in unmatched_entries[:10]:
                print(f"    {e.get('ticker', '?'):8s}  BUY  "
                      f"qty={e.get('quantity', '?')}  "
                      f"@ ${float(e.get('price', 0)):.2f}  "
                      f"[{e.get('timestamp', 'no time')}]")
            if len(unmatched_entries) > 10:
                print(f"    ... and {len(unmatched_entries) - 10} more")

        if unmatched_exits:
            print(f"  Orphaned exits (exit with no entry): {len(unmatched_exits)}")
            for e in unmatched_exits[:10]:
                print(f"    {e.get('ticker', '?'):8s}  EXIT  "
                      f"qty={e.get('quantity', '?')}  "
                      f"@ ${float(e.get('price', 0)):.2f}  "
                      f"[{e.get('timestamp', 'no time')}]")
            if len(unmatched_exits) > 10:
                print(f"    ... and {len(unmatched_exits) - 10} more")

    # Running P&L equity curve (text-based)
    if matched and len(matched) >= 3:
        print(f"\n--- EQUITY CURVE (cumulative P&L) ---")
        running = 0.0
        points = []
        for t in matched:
            running += t["pnl_total"]
            points.append(running)

        min_pnl = min(points)
        max_pnl = max(points)
        chart_width = 40

        if max_pnl != min_pnl:
            for i, val in enumerate(points):
                normalized = (val - min_pnl) / (max_pnl - min_pnl)
                bar_len = int(normalized * chart_width)
                bar = "#" * bar_len
                sign = "+" if val >= 0 else ""
                if i % max(1, len(points) // 20) == 0 or i == len(points) - 1:
                    print(f"  Trade {i+1:4d}: {sign}${val:>10,.2f} |{bar}")

    print(f"\n{'='*W}")


def export_csv(matched: list, output_path: Path) -> None:
    """Export matched trades to CSV for spreadsheet analysis."""
    if not matched:
        print("No trades to export.")
        return

    fieldnames = [
        "ticker", "entry_time", "exit_time", "entry_price", "exit_price",
        "quantity", "pnl_per_share", "pnl_total", "pnl_pct", "is_win",
        "exit_reason", "stop_loss", "take_profit",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(matched)

    print(f"\nCSV exported to: {output_path}")


def main():
    # Parse arguments
    input_file = str(DEFAULT_INPUT)
    do_csv = False

    for arg in sys.argv[1:]:
        if arg == "--csv":
            do_csv = True
        elif arg in ("--help", "-h"):
            print(__doc__)
            sys.exit(0)
        else:
            input_file = arg

    # Load records
    records = load_records(input_file)

    # Count action types
    actions = defaultdict(int)
    for r in records:
        actions[r.get("action", "unknown")] += 1
    print(f"Actions found: {dict(actions)}")

    # Match buys to exits
    matched, unmatched_entries, unmatched_exits = match_trades(records)
    print(f"Matched round-trip trades: {len(matched)}")
    print(f"Unmatched entries (still open): {len(unmatched_entries)}")
    print(f"Unmatched exits (orphaned): {len(unmatched_exits)}")

    # Calculate stats
    stats = calculate_stats(matched)
    ticker_perf = performance_by_ticker(matched)
    exit_perf = performance_by_exit_reason(matched)

    # Print report
    print_report(stats, ticker_perf, exit_perf, matched, unmatched_entries, unmatched_exits)

    # Save matched trades JSON
    matched_output = SCRIPT_DIR / "matched_trades.json"
    with open(matched_output, "w") as f:
        json.dump({
            "stats": stats,
            "ticker_performance": ticker_perf,
            "exit_performance": exit_perf,
            "trades": matched,
            "unmatched_entries": len(unmatched_entries),
            "unmatched_exits": len(unmatched_exits),
            "generated_at": datetime.now().isoformat(),
        }, f, indent=2, default=str)
    print(f"\nFull analysis saved to: {matched_output}")

    # Optional CSV export
    if do_csv:
        export_csv(matched, DEFAULT_CSV_OUTPUT)


if __name__ == "__main__":
    main()
