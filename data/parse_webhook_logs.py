#!/usr/bin/env python3
"""
Parse TradersPost webhook log data into structured JSON.

Input format (one entry per line):
    UUID  TIMESTAMP  JSON_PAYLOAD

Where:
    - UUID is a standard UUID like "a1b2c3d4-e5f6-..."
    - TIMESTAMP is ISO-8601 like "2025-02-14T10:30:00Z"
    - JSON_PAYLOAD is a complete JSON object starting with { and ending with }

Fields are separated by whitespace. The JSON payload may contain spaces
within its structure (e.g., in string values), so we locate it by finding
the first '{' character on each line.

Usage:
    python parse_webhook_logs.py <input_file> [output_file]
    python parse_webhook_logs.py webhook_logs.txt
    python parse_webhook_logs.py webhook_logs.txt tp_webhook_history.json

If output_file is omitted, defaults to tp_webhook_history.json in the same
directory as this script.
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

# Default output path
SCRIPT_DIR = Path(__file__).resolve().parent
DEFAULT_OUTPUT = SCRIPT_DIR / "tp_webhook_history.json"

# UUID regex pattern
UUID_PATTERN = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}"
)

# Flexible timestamp pattern: ISO-8601 with optional fractional seconds and timezone
TIMESTAMP_PATTERN = re.compile(
    r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?"
)


def parse_line(line: str, line_num: int) -> dict | None:
    """
    Parse a single log line into a structured dict.

    Returns None if the line cannot be parsed (blank, comment, etc.).
    """
    line = line.strip()
    if not line or line.startswith("#"):
        return None

    # Strategy 1: UUID + timestamp + JSON payload
    # Find the JSON payload by locating the first '{'
    json_start = line.find("{")
    if json_start == -1:
        print(f"  WARNING line {line_num}: No JSON payload found, skipping")
        return None

    prefix = line[:json_start].strip()
    json_str = line[json_start:]

    # Parse the JSON payload
    try:
        payload = json.loads(json_str)
    except json.JSONDecodeError as e:
        # Try to fix common issues: trailing commas, single quotes
        try:
            # Attempt to extract just the JSON object (in case of trailing text)
            brace_depth = 0
            end_idx = 0
            for i, ch in enumerate(json_str):
                if ch == "{":
                    brace_depth += 1
                elif ch == "}":
                    brace_depth -= 1
                    if brace_depth == 0:
                        end_idx = i + 1
                        break
            if end_idx > 0:
                payload = json.loads(json_str[:end_idx])
            else:
                print(f"  WARNING line {line_num}: Invalid JSON: {e}")
                return None
        except json.JSONDecodeError:
            print(f"  WARNING line {line_num}: Invalid JSON: {e}")
            return None

    # Extract UUID and timestamp from the prefix
    uuid_val = None
    timestamp_val = None

    uuid_match = UUID_PATTERN.search(prefix)
    if uuid_match:
        uuid_val = uuid_match.group(0)

    ts_match = TIMESTAMP_PATTERN.search(prefix)
    if ts_match:
        timestamp_val = ts_match.group(0)

    # If no UUID found, the prefix might just be a timestamp or other ID
    if not uuid_val and not timestamp_val:
        # Try treating the entire prefix as space-separated tokens
        tokens = prefix.split()
        if len(tokens) >= 2:
            uuid_val = tokens[0]
            timestamp_val = " ".join(tokens[1:])
        elif len(tokens) == 1:
            # Could be either UUID or timestamp
            if TIMESTAMP_PATTERN.match(tokens[0]):
                timestamp_val = tokens[0]
            else:
                uuid_val = tokens[0]

    # Build the structured record
    record = {
        "log_id": uuid_val,
        "timestamp": timestamp_val,
        "ticker": payload.get("ticker", ""),
        "action": payload.get("action", ""),
        "sentiment": payload.get("sentiment", ""),
        "quantity": payload.get("quantity", 0),
        "price": payload.get("price", 0),
    }

    # Include stop loss / take profit if present
    if "stopLoss" in payload:
        sl = payload["stopLoss"]
        if isinstance(sl, dict):
            record["stop_loss"] = sl.get("stopPrice", 0)
        else:
            record["stop_loss"] = sl

    if "takeProfit" in payload:
        tp = payload["takeProfit"]
        if isinstance(tp, dict):
            record["take_profit"] = tp.get("limitPrice", 0)
        else:
            record["take_profit"] = tp

    # Preserve the full original payload for reference
    record["raw_payload"] = payload

    return record


def parse_file(input_path: str) -> list[dict]:
    """Parse an entire webhook log file and return list of structured records."""
    records = []
    path = Path(input_path)

    if not path.exists():
        print(f"ERROR: Input file not found: {path}")
        sys.exit(1)

    print(f"Parsing: {path}")
    print(f"File size: {path.stat().st_size:,} bytes")

    with open(path, "r", encoding="utf-8", errors="replace") as f:
        lines = f.readlines()

    print(f"Total lines: {len(lines)}")

    skipped = 0
    for i, line in enumerate(lines, start=1):
        record = parse_line(line, i)
        if record is not None:
            record["line_number"] = i
            records.append(record)
        else:
            if line.strip() and not line.strip().startswith("#"):
                skipped += 1

    print(f"Parsed: {len(records)} records")
    if skipped:
        print(f"Skipped: {skipped} unparseable lines")

    return records


def summarize(records: list[dict]) -> None:
    """Print a quick summary of parsed data."""
    if not records:
        print("\nNo records to summarize.")
        return

    actions = {}
    tickers = {}
    for r in records:
        act = r.get("action", "unknown")
        actions[act] = actions.get(act, 0) + 1
        tick = r.get("ticker", "unknown")
        tickers[tick] = tickers.get(tick, 0) + 1

    print(f"\n{'='*50}")
    print(f"SUMMARY")
    print(f"{'='*50}")
    print(f"Total records: {len(records)}")
    print(f"\nActions:")
    for act, count in sorted(actions.items(), key=lambda x: -x[1]):
        print(f"  {act:10s}: {count}")
    print(f"\nUnique tickers: {len(tickers)}")
    print(f"Top tickers:")
    for tick, count in sorted(tickers.items(), key=lambda x: -x[1])[:15]:
        print(f"  {tick:8s}: {count} signals")

    # Date range
    timestamps = [r["timestamp"] for r in records if r.get("timestamp")]
    if timestamps:
        print(f"\nDate range: {min(timestamps)} to {max(timestamps)}")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        print("Error: Please provide an input file path.")
        print(f"\nUsage: python {Path(__file__).name} <input_file> [output_file]")
        sys.exit(1)

    input_file = sys.argv[1]
    output_file = sys.argv[2] if len(sys.argv) > 2 else str(DEFAULT_OUTPUT)

    records = parse_file(input_file)

    if not records:
        print("No records parsed. Check the input file format.")
        sys.exit(1)

    # Save to JSON
    output_path = Path(output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(records, f, indent=2, default=str)

    print(f"\nSaved to: {output_path}")
    print(f"Output size: {output_path.stat().st_size:,} bytes")

    summarize(records)

    print(f"\nNext step: Run analyze_trades.py to match buys/exits and calculate P&L:")
    print(f"  python {SCRIPT_DIR / 'analyze_trades.py'}")


if __name__ == "__main__":
    main()
