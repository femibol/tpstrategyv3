import asyncio

# Ensure an event loop exists before ib_insync/eventkit is imported
# (Python 3.10+ no longer auto-creates one in the main thread)
try:
    asyncio.get_event_loop()
except RuntimeError:
    asyncio.set_event_loop(asyncio.new_event_loop())

from bot.brokers.base import BaseBroker
from bot.brokers.ibkr import IBKRBroker
from bot.brokers.traderspost import TradersPostBroker
