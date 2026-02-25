"""
Google Sheets Trade Logger - Syncs all trades to a Google Sheet.

Setup:
1. Create a Google Cloud project and enable the Google Sheets API
2. Create a Service Account and download the JSON key file
3. Share your Google Sheet with the service account email
4. Set environment variables:
   - GOOGLE_SHEETS_CREDENTIALS: path to service account JSON key file
   - GOOGLE_SHEETS_SPREADSHEET_ID: the spreadsheet ID from the sheet URL
"""
import os
import json
from datetime import datetime
from pathlib import Path

from bot.utils.logger import get_logger

log = get_logger("integrations.google_sheets")

# Lazy imports — gspread and google-auth pull in cryptography which can
# crash with a Rust/pyo3 panic on some systems.  Defer to _connect() so a
# broken cryptography library doesn't prevent the entire bot from starting.
gspread = None
Credentials = None


# Column headers for the trade log sheet
TRADE_HEADERS = [
    "Date", "Time", "Symbol", "Direction", "Strategy",
    "Entry Price", "Exit Price", "Quantity", "P&L ($)", "P&L (%)",
    "Exit Reason", "Executed Via", "Hold Time", "Entry Time", "Exit Time",
]


class GoogleSheetsLogger:
    """Logs all completed trades to a Google Sheet for review and analysis."""

    def __init__(self, config):
        self.config = config
        self._client = None
        self._sheet = None
        self._worksheet = None
        self._enabled = False
        self._credentials_path = os.getenv("GOOGLE_SHEETS_CREDENTIALS", "")
        self._spreadsheet_id = os.getenv("GOOGLE_SHEETS_SPREADSHEET_ID", "")

        if not self._credentials_path or not self._spreadsheet_id:
            log.info(
                "Google Sheets disabled: set GOOGLE_SHEETS_CREDENTIALS and "
                "GOOGLE_SHEETS_SPREADSHEET_ID env vars"
            )
            return

        self._connect()

    def _connect(self):
        """Connect to Google Sheets API."""
        global gspread, Credentials
        try:
            if gspread is None:
                import gspread as _gspread
                from google.oauth2.service_account import Credentials as _Credentials
                gspread = _gspread
                Credentials = _Credentials

            scopes = [
                "https://www.googleapis.com/auth/spreadsheets",
                "https://www.googleapis.com/auth/drive",
            ]

            # Support both file path and JSON string for credentials
            if os.path.isfile(self._credentials_path):
                creds = Credentials.from_service_account_file(
                    self._credentials_path, scopes=scopes
                )
            else:
                # Try parsing as JSON string (useful for Render env vars)
                creds_dict = json.loads(self._credentials_path)
                creds = Credentials.from_service_account_info(
                    creds_dict, scopes=scopes
                )

            self._client = gspread.authorize(creds)
            self._sheet = self._client.open_by_key(self._spreadsheet_id)

            # Get or create the "Trades" worksheet
            try:
                self._worksheet = self._sheet.worksheet("Trades")
            except gspread.WorksheetNotFound:
                self._worksheet = self._sheet.add_worksheet(
                    title="Trades", rows=1000, cols=len(TRADE_HEADERS)
                )
                # Write headers
                self._worksheet.append_row(TRADE_HEADERS)

            self._enabled = True
            log.info(
                f"Google Sheets connected: {self._sheet.title} | "
                f"Worksheet: Trades ({self._worksheet.row_count} rows)"
            )
        except Exception as e:
            log.warning(f"Google Sheets connection failed: {e}")
            self._enabled = False

    def is_enabled(self):
        return self._enabled

    def log_trade(self, trade):
        """Log a completed trade to the Google Sheet.

        Args:
            trade: dict with keys: symbol, direction, entry_price, exit_price,
                   quantity, pnl, pnl_pct, strategy, reason, executed_via,
                   entry_time, exit_time
        """
        if not self._enabled:
            return False

        try:
            # Parse times
            entry_time = trade.get("entry_time", "")
            exit_time = trade.get("exit_time", "")

            # Calculate hold time
            hold_time = ""
            try:
                if entry_time and exit_time:
                    et = datetime.fromisoformat(entry_time)
                    xt = datetime.fromisoformat(exit_time)
                    delta = xt - et
                    hours = delta.total_seconds() / 3600
                    if hours >= 24:
                        hold_time = f"{hours/24:.1f}d"
                    elif hours >= 1:
                        hold_time = f"{hours:.1f}h"
                    else:
                        hold_time = f"{delta.total_seconds()/60:.0f}m"
            except Exception:
                pass

            # Format date/time from exit_time
            try:
                exit_dt = datetime.fromisoformat(exit_time)
                date_str = exit_dt.strftime("%Y-%m-%d")
                time_str = exit_dt.strftime("%H:%M:%S")
            except Exception:
                date_str = datetime.now().strftime("%Y-%m-%d")
                time_str = datetime.now().strftime("%H:%M:%S")

            row = [
                date_str,
                time_str,
                trade.get("symbol", ""),
                trade.get("direction", "long"),
                trade.get("strategy", ""),
                round(trade.get("entry_price", 0), 2),
                round(trade.get("exit_price", 0), 2),
                trade.get("quantity", 0),
                round(trade.get("pnl", 0), 2),
                f"{trade.get('pnl_pct', 0) * 100:.2f}%",
                trade.get("reason", ""),
                trade.get("executed_via", ""),
                hold_time,
                entry_time,
                exit_time,
            ]

            self._worksheet.append_row(row, value_input_option="USER_ENTERED")
            log.debug(f"Trade logged to Google Sheets: {trade.get('symbol')} P&L ${trade.get('pnl', 0):+.2f}")
            return True

        except Exception as e:
            log.warning(f"Google Sheets log failed: {e}")
            # Try reconnecting on auth errors
            if "401" in str(e) or "403" in str(e) or "token" in str(e).lower():
                self._connect()
            return False

    def log_daily_summary(self, summary):
        """Log end-of-day summary to a 'Daily' worksheet.

        Args:
            summary: dict with keys: date, trades, wins, losses, pnl,
                     balance, positions_held, best_trade, worst_trade
        """
        if not self._enabled:
            return False

        try:
            # Get or create Daily worksheet
            try:
                daily_ws = self._sheet.worksheet("Daily")
            except gspread.WorksheetNotFound:
                daily_ws = self._sheet.add_worksheet(title="Daily", rows=500, cols=10)
                daily_ws.append_row([
                    "Date", "Trades", "Wins", "Losses", "Win Rate",
                    "P&L ($)", "Balance", "Best Trade", "Worst Trade", "Positions EOD",
                ])

            win_rate = ""
            total = summary.get("trades", 0)
            wins = summary.get("wins", 0)
            if total > 0:
                win_rate = f"{wins/total*100:.0f}%"

            row = [
                summary.get("date", datetime.now().strftime("%Y-%m-%d")),
                summary.get("trades", 0),
                summary.get("wins", 0),
                summary.get("losses", 0),
                win_rate,
                round(summary.get("pnl", 0), 2),
                round(summary.get("balance", 0), 2),
                summary.get("best_trade", ""),
                summary.get("worst_trade", ""),
                summary.get("positions_held", 0),
            ]

            daily_ws.append_row(row, value_input_option="USER_ENTERED")
            log.info(f"Daily summary logged to Google Sheets: {summary.get('date')}")
            return True

        except Exception as e:
            log.warning(f"Google Sheets daily summary failed: {e}")
            return False

    def sync_all_trades(self, trades):
        """Bulk sync all trades from history (for backfill).

        Args:
            trades: list of trade dicts
        """
        if not self._enabled or not trades:
            return False

        try:
            for trade in trades:
                self.log_trade(trade)
            log.info(f"Synced {len(trades)} trades to Google Sheets")
            return True
        except Exception as e:
            log.warning(f"Google Sheets bulk sync failed: {e}")
            return False
