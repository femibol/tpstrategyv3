#!/usr/bin/env python3
"""
IBKR Test Order — verifies connection and order placement.

Places a 1-share BUY of AAPL via IBKR paper trading (port 7497),
waits for fill, then immediately sells to close.

Usage:
    python test_ibkr_order.py
"""
import sys
import os
import time
import asyncio

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Python 3.10+ no longer auto-creates an event loop.
# ib_insync / eventkit needs one at import time.
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from ib_async import IB, Stock, MarketOrder
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")
log = logging.getLogger("test_order")

# Silence ib_insync noise
logging.getLogger('ib_async.wrapper').setLevel(logging.CRITICAL)
logging.getLogger('ib_async.ib').setLevel(logging.CRITICAL)

HOST = os.getenv("IBKR_HOST", "127.0.0.1")
PORT = int(os.getenv("IBKR_PORT", "7497"))
CLIENT_ID = 99  # Use different client ID to avoid conflicts with running bot

SYMBOL = "AAPL"
QTY = 1


def main():
    ib = IB()

    # 1. Connect
    log.info(f"Connecting to IBKR at {HOST}:{PORT} (client {CLIENT_ID})...")
    try:
        ib.connect(HOST, PORT, clientId=CLIENT_ID, timeout=10)
    except Exception as e:
        log.error(f"Connection FAILED: {e}")
        log.info("Make sure TWS/Gateway is running and API is enabled")
        return False

    mode = "PAPER" if PORT in (7497, 4002) else "LIVE"
    log.info(f"Connected to IBKR ({mode})")

    if mode == "LIVE":
        log.error("SAFETY: This test only runs on PAPER trading. Aborting.")
        ib.disconnect()
        return False

    # 2. Qualify contract
    log.info(f"Qualifying {SYMBOL}...")
    contract = Stock(SYMBOL, "SMART", "USD")
    ib.qualifyContracts(contract)
    if contract.conId == 0:
        log.error(f"Failed to qualify {SYMBOL}")
        ib.disconnect()
        return False
    log.info(f"Qualified: {contract} (conId={contract.conId})")

    # 3. Get current price
    ticker = ib.reqMktData(contract, snapshot=True)
    ib.sleep(2)
    price = ticker.marketPrice()
    log.info(f"Current {SYMBOL} price: ${price:.2f}")

    # 4. Place BUY order (1 share, market)
    log.info(f"Placing BUY {QTY} {SYMBOL} @ MARKET...")
    buy_order = MarketOrder("BUY", QTY)
    buy_order.tif = "DAY"
    trade = ib.placeOrder(contract, buy_order)

    # Wait for fill
    for i in range(10):
        ib.sleep(1)
        status = trade.orderStatus.status
        log.info(f"  Order status: {status}")
        if status in ("Filled", "Cancelled", "ApiCancelled"):
            break

    if trade.orderStatus.status == "Filled":
        fill_price = trade.orderStatus.avgFillPrice
        log.info(f"BUY FILLED @ ${fill_price:.2f}")
    else:
        log.warning(f"BUY order status: {trade.orderStatus.status}")
        log.info("Cancelling...")
        ib.cancelOrder(buy_order)
        ib.sleep(1)
        ib.disconnect()
        return False

    # 5. Immediately SELL to close
    log.info(f"Closing: SELL {QTY} {SYMBOL} @ MARKET...")
    sell_order = MarketOrder("SELL", QTY)
    sell_order.tif = "DAY"
    sell_trade = ib.placeOrder(contract, sell_order)

    for i in range(10):
        ib.sleep(1)
        status = sell_trade.orderStatus.status
        log.info(f"  Sell status: {status}")
        if status in ("Filled", "Cancelled", "ApiCancelled"):
            break

    if sell_trade.orderStatus.status == "Filled":
        sell_price = sell_trade.orderStatus.avgFillPrice
        pnl = (sell_price - fill_price) * QTY
        log.info(f"SELL FILLED @ ${sell_price:.2f} | P&L: ${pnl:+.2f}")
    else:
        log.warning(f"SELL order status: {sell_trade.orderStatus.status}")

    # 6. Done
    log.info("TEST COMPLETE - IBKR order placement works!")
    ib.disconnect()
    return True


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
