#!/usr/bin/env python3
"""
Build tp_webhook_history.json from pasted webhook data.
Reads raw_webhook_data.txt (UUID TIMESTAMP PAYLOAD format) and outputs JSON.

Usage:
    python data/build_history_from_paste.py < data/raw_webhook_data.txt
    # Or just:
    python data/build_history_from_paste.py data/raw_webhook_data.txt
"""
import json
import re
import sys
from pathlib import Path


def parse_line(line):
    """Parse a webhook log line: UUID TIMESTAMP JSON_PAYLOAD"""
    line = line.strip()
    if not line:
        return None

    # Find the JSON payload (starts with {)
    json_start = line.find('{')
    if json_start == -1:
        return None

    # Extract parts before the JSON
    prefix = line[:json_start].strip()
    json_str = line[json_start:]

    # Split prefix into UUID and timestamp
    parts = prefix.split()
    if len(parts) < 2:
        return None

    uuid = parts[0]
    timestamp = parts[1]

    try:
        payload = json.loads(json_str)
    except json.JSONDecodeError:
        return None

    payload["uuid"] = uuid
    payload["timestamp"] = timestamp
    return payload


def main():
    data_dir = Path(__file__).parent
    output_file = data_dir / "tp_webhook_history.json"

    # Read from file argument or stdin
    if len(sys.argv) > 1 and sys.argv[1] != '-':
        input_path = Path(sys.argv[1])
        if not input_path.exists():
            print(f"Error: {input_path} not found")
            sys.exit(1)
        with open(input_path) as f:
            lines = f.readlines()
    else:
        print("Reading from stdin (paste data, then Ctrl+D)...")
        lines = sys.stdin.readlines()

    signals = []
    for line in lines:
        parsed = parse_line(line)
        if parsed:
            signals.append(parsed)

    # Data is already in chronological order (oldest first) from the paste
    # No reversal needed

    with open(output_file, 'w') as f:
        json.dump(signals, f, indent=2)

    print(f"Parsed {len(signals)} signals -> {output_file}")

    # Quick stats
    buys = [s for s in signals if s.get("action") == "buy"]
    exits = [s for s in signals if s.get("action") == "exit"]
    tickers = set(s.get("ticker", "") for s in signals)
    print(f"  Buys: {len(buys)}  |  Exits: {len(exits)}  |  Tickers: {len(tickers)}")


if __name__ == "__main__":
    main()
