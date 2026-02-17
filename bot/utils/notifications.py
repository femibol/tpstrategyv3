"""
Notification system - Rich Discord embeds, dashboard alerts, and console logging.
Every alert is data-rich so you know exactly what's happening at a glance.
"""
import json
import os
from datetime import datetime

import requests

from bot.utils.logger import get_logger

log = get_logger("notifications")


class Notifier:
    """Send rich trade alerts and system notifications."""

    def __init__(self, config):
        self.config = config
        self.discord_url = config.discord_webhook_url
        self.history = []

    # =========================================================================
    # Scanner Cycle Summary
    # =========================================================================

    def scanner_summary(self, symbols_scanned, signals_found, regime=None,
                        spy_change=None, approved=None, rejected=None):
        """Rich scanner cycle notification."""
        regime_str = regime.upper() if regime else "UNKNOWN"
        regime_icon = {"BULLISH": "🟢", "BEARISH": "🔴", "SIDEWAYS": "🟡",
                       "CRISIS": "🚨", "VOLATILE": "⚡"}.get(regime_str, "⚪")

        spy_str = f"SPY: {spy_change:+.2f}%" if spy_change is not None else ""

        lines = [
            f"📡 **[SCANNER]** Scanning {symbols_scanned} symbols",
            f"Market: {regime_icon} {regime_str} {spy_str}",
        ]

        if signals_found:
            lines.append(f"Signals found: {len(signals_found)}")
            for sig in signals_found[:5]:
                sym = sig.get("symbol", "?")
                conf = sig.get("confidence", 0) * 100
                reason = sig.get("reason", "")[:60]
                lines.append(f"  ✅ {sym}: score {conf:.0f}/100 — {reason}")

        if approved:
            lines.append(f"Approved: {len(approved)} trades")
        if rejected:
            lines.append(f"Filtered: {rejected} signals rejected by risk manager")

        msg = "\n".join(lines)
        self._send(msg, title="Scanner Cycle", category="scanner")

    # =========================================================================
    # Trade Entry (Rich)
    # =========================================================================

    def trade_entry(self, symbol, action, qty, price, stop_loss, take_profit,
                    strategy, reason="", confidence=0, rr_ratio=0,
                    executed_via="Simulated", rvol=None, targets=None):
        """Rich trade entry notification with full details."""
        value = qty * price
        risk_dollars = abs(price - stop_loss) * qty
        reward_dollars = abs(take_profit - price) * qty
        risk_pct = abs(price - stop_loss) / price * 100 if price > 0 else 0

        # Build multi-target string
        if targets and len(targets) > 0:
            target_str = ", ".join([f"${t:.2f}" for t in targets[:3]])
        else:
            target_str = f"${take_profit:.2f}"

        lines = [
            f"{'🟢' if action.upper() in ('BUY',) else '🔴'} **>>> ENTERING POSITION: {symbol}**",
            f"```",
            f"  Entry: ${price:.2f} x {qty} shares = ${value:,.2f}",
            f"  Stop:  ${stop_loss:.2f} (-${risk_dollars:.2f} | -{risk_pct:.1f}%)",
            f"  Targets: {target_str}",
            f"  Risk/Reward: {rr_ratio:.1f}",
            f"```",
            f"Strategy: **{strategy}** | Confidence: **{confidence * 100:.0f}%**",
            f"Reason: {reason}",
        ]

        if rvol and rvol > 1.5:
            lines.append(f"RVOL: **{rvol:.1f}x** avg volume")

        lines.append(f"Via: {executed_via} | {datetime.now().strftime('%H:%M:%S ET')}")

        msg = "\n".join(lines)
        self._send(msg, title="Trade Entry", category="trade")

        # Also send Discord embed for richer formatting
        if self.discord_url:
            self._send_discord_embed(
                title=f"{'🟢' if action.upper() == 'BUY' else '🔴'} {action.upper()} {symbol}",
                color=0x3FB950 if action.upper() == "BUY" else 0xF85149,
                fields=[
                    {"name": "Entry", "value": f"${price:.2f} x {qty}", "inline": True},
                    {"name": "Value", "value": f"${value:,.2f}", "inline": True},
                    {"name": "Stop Loss", "value": f"${stop_loss:.2f} (-${risk_dollars:.2f})", "inline": True},
                    {"name": "Targets", "value": target_str, "inline": True},
                    {"name": "R/R", "value": f"{rr_ratio:.1f}", "inline": True},
                    {"name": "Confidence", "value": f"{confidence * 100:.0f}%", "inline": True},
                    {"name": "Strategy", "value": strategy, "inline": True},
                    {"name": "Via", "value": executed_via, "inline": True},
                    {"name": "RVOL", "value": f"{rvol:.1f}x" if rvol else "—", "inline": True},
                    {"name": "Reason", "value": reason[:200], "inline": False},
                ],
            )

    # =========================================================================
    # Trade Exit (Rich)
    # =========================================================================

    def trade_exit(self, symbol, direction, qty, entry_price, exit_price,
                   pnl, pnl_pct, reason_type, reason_msg, strategy,
                   executed_via="Simulated", hold_time=None):
        """Rich trade exit notification."""
        win = pnl > 0
        icon = "💰" if win else "💸"
        value = qty * exit_price

        hold_str = ""
        if hold_time:
            minutes = hold_time.total_seconds() / 60
            if minutes > 60:
                hold_str = f"{minutes / 60:.1f}h"
            else:
                hold_str = f"{minutes:.0f}m"

        lines = [
            f"{icon} **<<< CLOSED: {symbol}** ({reason_type})",
            f"```",
            f"  Entry: ${entry_price:.2f} → Exit: ${exit_price:.2f}",
            f"  P&L: ${pnl:+.2f} ({pnl_pct:+.2f}%)",
            f"  Shares: {qty} | Value: ${value:,.2f}",
            f"  Hold: {hold_str}" if hold_str else "",
            f"```",
            f"Strategy: **{strategy}** | {reason_msg}",
            f"Via: {executed_via} | {datetime.now().strftime('%H:%M:%S ET')}",
        ]
        lines = [l for l in lines if l]  # Remove empty

        msg = "\n".join(lines)
        self._send(msg, title="Trade Exit", category="trade")

        if self.discord_url:
            self._send_discord_embed(
                title=f"{icon} CLOSED {symbol} — ${pnl:+.2f}",
                color=0x3FB950 if win else 0xF85149,
                fields=[
                    {"name": "Entry → Exit", "value": f"${entry_price:.2f} → ${exit_price:.2f}", "inline": True},
                    {"name": "P&L", "value": f"${pnl:+.2f} ({pnl_pct:+.2f}%)", "inline": True},
                    {"name": "Shares", "value": str(qty), "inline": True},
                    {"name": "Reason", "value": reason_type, "inline": True},
                    {"name": "Hold Time", "value": hold_str or "—", "inline": True},
                    {"name": "Strategy", "value": strategy, "inline": True},
                ],
            )

    # =========================================================================
    # Partial Close
    # =========================================================================

    def trade_partial(self, symbol, qty_closed, qty_remaining, price, pnl,
                      target_idx, target_pct, strategy):
        """Rich partial close notification."""
        lines = [
            f"📐 **PARTIAL CLOSE: {symbol}**",
            f"```",
            f"  Closed: {qty_closed} shares @ ${price:.2f}",
            f"  P&L: ${pnl:+.2f} (target {target_idx + 1}: +{target_pct:.0%})",
            f"  Remaining: {qty_remaining} shares",
            f"```",
            f"Strategy: {strategy} | {datetime.now().strftime('%H:%M:%S ET')}",
        ]
        msg = "\n".join(lines)
        self._send(msg, title="Partial Close", category="trade")

    # =========================================================================
    # Position Update (Stop Move, Break-Even, Trailing)
    # =========================================================================

    def position_update(self, symbol, update_type, details):
        """Notify on position management changes."""
        icons = {
            "breakeven": "🛡️",
            "trailing_tightened": "🔒",
            "stop_moved": "📍",
        }
        icon = icons.get(update_type, "📋")
        msg = f"{icon} **{update_type.upper().replace('_', ' ')}: {symbol}**\n{details}"
        self._send(msg, title="Position Update", category="position")

    # =========================================================================
    # Legacy compatibility (keep old interface working)
    # =========================================================================

    def trade_alert(self, action, symbol, qty, price, strategy, reason=""):
        """Legacy trade alert - still works but with enhanced formatting."""
        value = qty * price
        emoji = "🟢" if action.upper() in ("BUY",) else "🔴" if action.upper() in ("SELL", "CLOSE") else "📐"
        msg = (
            f"{emoji} **{action.upper()} {symbol}**\n"
            f"```\n"
            f"  Qty: {qty} | Price: ${price:.2f} | Value: ${value:,.2f}\n"
            f"```\n"
            f"Strategy: {strategy}\n"
            f"Reason: {reason}\n"
            f"Time: {datetime.now().strftime('%H:%M:%S ET')}"
        )
        self._send(msg, title="Trade", category="trade")

    def risk_alert(self, message):
        """Send risk management alert."""
        msg = f"🚨 **RISK ALERT**\n```\n{message}\n```"
        self._send(msg, title="Risk Alert", category="risk")

        if self.discord_url:
            self._send_discord_embed(
                title="🚨 RISK ALERT",
                color=0xF85149,
                fields=[{"name": "Details", "value": message, "inline": False}],
            )

    def daily_summary(self, stats):
        """Rich end-of-day performance summary."""
        pnl = stats.get("pnl", 0)
        pnl_icon = "📈" if pnl > 0 else "📉" if pnl < 0 else "➡️"

        msg = (
            f"{pnl_icon} **DAILY SUMMARY — {datetime.now().strftime('%Y-%m-%d')}**\n"
            f"```\n"
            f"  P&L:      ${pnl:+.2f} ({stats.get('pnl_pct', 0):+.2f}%)\n"
            f"  Trades:   {stats.get('trades', 0)}\n"
            f"  Win Rate: {stats.get('win_rate', 0):.0f}%\n"
            f"  Balance:  ${stats.get('balance', 0):,.2f}\n"
            f"  Open Pos: {stats.get('open_positions', 0)}\n"
            f"  O/N Hold: {stats.get('overnight_holds', 0)}\n"
            f"  Regime:   {stats.get('regime', 'N/A').upper()}\n"
            f"```"
        )
        self._send(msg, title="Daily Summary", category="summary")

        if self.discord_url:
            self._send_discord_embed(
                title=f"{pnl_icon} Daily Summary — {datetime.now().strftime('%Y-%m-%d')}",
                color=0x3FB950 if pnl > 0 else 0xF85149 if pnl < 0 else 0x8B949E,
                fields=[
                    {"name": "P&L", "value": f"${pnl:+.2f} ({stats.get('pnl_pct', 0):+.2f}%)", "inline": True},
                    {"name": "Trades", "value": str(stats.get("trades", 0)), "inline": True},
                    {"name": "Win Rate", "value": f"{stats.get('win_rate', 0):.0f}%", "inline": True},
                    {"name": "Balance", "value": f"${stats.get('balance', 0):,.2f}", "inline": True},
                    {"name": "Open", "value": str(stats.get("open_positions", 0)), "inline": True},
                    {"name": "Regime", "value": stats.get("regime", "N/A").upper(), "inline": True},
                ],
            )

    def system_alert(self, message, level="info"):
        """Send system status alert."""
        icons = {"info": "ℹ️", "warning": "⚠️", "error": "🚨", "success": "✅"}
        icon = icons.get(level, "ℹ️")
        msg = f"{icon} **System**: {message}"
        self._send(msg, title="System", category="system")

    # =========================================================================
    # Internal routing
    # =========================================================================

    def _send(self, message, title="", category=""):
        """Route notification to all configured channels."""
        log.info(f"[ALERT] {title}: {message[:200]}")
        self.history.append({
            "time": datetime.now().isoformat(),
            "title": title,
            "message": message,
            "category": category,
        })

        # Trim history
        if len(self.history) > 100:
            self.history = self.history[-100:]

        if self.discord_url:
            self._send_discord(message)

    def _send_discord(self, message):
        """Send plain text notification to Discord webhook."""
        try:
            # Discord has 2000 char limit
            if len(message) > 1950:
                message = message[:1950] + "..."

            payload = {
                "content": message,
                "username": "AlgoBot",
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

    def _send_discord_embed(self, title, color=0x58A6FF, fields=None,
                            description=None, footer=None):
        """Send rich Discord embed notification."""
        if not self.discord_url:
            return

        try:
            embed = {
                "title": title,
                "color": color,
                "timestamp": datetime.utcnow().isoformat(),
                "footer": {"text": footer or "AlgoBot"},
            }

            if description:
                embed["description"] = description

            if fields:
                embed["fields"] = fields

            payload = {
                "username": "AlgoBot",
                "embeds": [embed],
            }

            resp = requests.post(
                self.discord_url,
                json=payload,
                timeout=10
            )
            if resp.status_code not in (200, 204):
                log.warning(f"Discord embed returned {resp.status_code}")
        except Exception as e:
            log.error(f"Discord embed failed: {e}")

    def get_history(self, count=20, category=None):
        """Get recent notification history, optionally filtered by category."""
        history = self.history
        if category:
            history = [h for h in history if h.get("category") == category]
        return history[-count:]
