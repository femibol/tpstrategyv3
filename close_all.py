#!/usr/bin/env python3
"""
Emergency flatten: Close ALL positions in IBKR and cancel all open orders.
Usage: python close_all.py
"""
import os
import sys
import time

# Load env
from dotenv import load_dotenv
load_dotenv()

from bot.brokers.ibkr import IBKRBroker

HOST = os.getenv("IBKR_HOST", "127.0.0.1")
PORT = int(os.getenv("IBKR_PORT", 7497))
CLIENT_ID = int(os.getenv("IBKR_CLIENT_ID", 1)) + 99  # Use different client ID to avoid conflict

print(f"Connecting to IBKR at {HOST}:{PORT} (clientId={CLIENT_ID})...")
broker = IBKRBroker(host=HOST, port=PORT, client_id=CLIENT_ID)

if not broker.connect():
    print("ERROR: Could not connect to IBKR. Is TWS/Gateway running?")
    sys.exit(1)

positions = broker.get_positions()
if not positions:
    print("No open positions found.")
    broker.disconnect()
    sys.exit(0)

print(f"\n{'='*60}")
print(f"POSITIONS TO CLOSE ({len(positions)}):")
print(f"{'='*60}")
for sym, pos in positions.items():
    direction = pos['direction'].upper()
    qty = pos['quantity']
    avg = pos.get('avg_cost', 0)
    print(f"  {direction:5s}  {qty:>6}  {sym:<8s}  @ ${avg:.2f}")
print(f"{'='*60}")

answer = input("\nType 'FLATTEN' to close ALL positions: ").strip()
if answer != "FLATTEN":
    print("Aborted.")
    broker.disconnect()
    sys.exit(0)

print("\nFlattening all positions...")
broker.close_all_positions()

# Wait and verify
time.sleep(3)
remaining = broker.get_positions()
if remaining:
    print(f"\nWARNING: {len(remaining)} positions still open:")
    for sym, pos in remaining.items():
        print(f"  {pos['direction'].upper():5s}  {pos['quantity']:>6}  {sym}")
else:
    print("\nAll positions closed successfully.")

broker.disconnect()
