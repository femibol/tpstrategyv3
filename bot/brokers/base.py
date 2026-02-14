"""
Base broker interface - all broker implementations inherit from this.
"""
from abc import ABC, abstractmethod


class BaseBroker(ABC):
    """Abstract broker interface."""

    @abstractmethod
    def connect(self):
        """Connect to broker. Returns True if successful."""
        pass

    @abstractmethod
    def disconnect(self):
        """Disconnect from broker."""
        pass

    @abstractmethod
    def is_connected(self):
        """Check if connected."""
        pass

    @abstractmethod
    def reconnect(self):
        """Reconnect to broker."""
        pass

    @abstractmethod
    def place_order(self, symbol, action, quantity, order_type="LIMIT",
                    limit_price=None, stop_price=None):
        """Place an order. Returns order dict or None."""
        pass

    @abstractmethod
    def cancel_order(self, order_id):
        """Cancel an order."""
        pass

    @abstractmethod
    def get_positions(self):
        """Get all open positions. Returns dict of symbol -> position."""
        pass

    @abstractmethod
    def get_account_summary(self):
        """Get account summary. Returns dict with balance info."""
        pass

    @abstractmethod
    def get_order_status(self, order_id):
        """Get status of an order."""
        pass
