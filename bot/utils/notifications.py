"""
Notification system - Discord, email, and console alerts.
"""
import json
import smtplib
import os
from email.mime.text import MIMEText
from datetime import datetime

import requests

from bot.utils.logger import get_logger

log = get_logger("notifications")


class Notifier:
    """Send trade alerts and system notifications."""

    def __init__(self, config):
        self.config = config
        self.discord_url = config.discord_webhook_url
        self.history = []

    def trade_alert(self, action, symbol, qty, price, strategy, reason=""):
        """Send alert for trade execution."""
        emoji = "🟢" if action.upper() == "BUY" else "🔴"
        msg = (
            f"{emoji} **{action.upper()} {symbol}**\n"
            f"Qty: {qty} | Price: ${price:.2f} | Value: ${qty * price:.2f}\n"
            f"Strategy: {strategy}\n"
            f"Reason: {reason}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S ET')}"
        )
        self._send(msg, title="Trade Executed")

    def risk_alert(self, message):
        """Send risk management alert."""
        msg = f"⚠️ **RISK ALERT**\n{message}"
        self._send(msg, title="Risk Alert")

    def daily_summary(self, stats):
        """Send end-of-day performance summary."""
        msg = (
            f"📊 **Daily Summary - {datetime.now().strftime('%Y-%m-%d')}**\n"
            f"P&L: ${stats.get('pnl', 0):+.2f} "
            f"({stats.get('pnl_pct', 0):+.2f}%)\n"
            f"Trades: {stats.get('trades', 0)} | "
            f"Win Rate: {stats.get('win_rate', 0):.0f}%\n"
            f"Balance: ${stats.get('balance', 0):,.2f}\n"
            f"Open Positions: {stats.get('open_positions', 0)}"
        )
        self._send(msg, title="Daily Summary")

    def system_alert(self, message, level="info"):
        """Send system status alert."""
        icons = {"info": "ℹ️", "warning": "⚠️", "error": "🚨", "success": "✅"}
        icon = icons.get(level, "ℹ️")
        msg = f"{icon} **System**: {message}"
        self._send(msg, title="System Alert")

    def _send(self, message, title=""):
        """Route notification to all configured channels."""
        log.info(f"[ALERT] {title}: {message}")
        self.history.append({
            "time": datetime.now().isoformat(),
            "title": title,
            "message": message
        })

        if self.discord_url:
            self._send_discord(message)

    def _send_discord(self, message):
        """Send notification to Discord webhook."""
        try:
            payload = {
                "content": message,
                "username": "Trading Bot"
            }
            resp = requests.post(
                self.discord_url,
                json=payload,
                timeout=10
            )
            if resp.status_code not in (200, 204):
                log.warning(f"Discord webhook returned {resp.status_code}")
        except Exception as e:
            log.error(f"Discord notification failed: {e}")
