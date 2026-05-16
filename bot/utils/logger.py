"""
Logging setup for the trading bot.
"""
import os
import sys
import logging
import logging.handlers
from pathlib import Path


class SafeStreamHandler(logging.StreamHandler):
    """StreamHandler that never raises UnicodeEncodeError on Windows cp1252."""

    def emit(self, record):
        try:
            super().emit(record)
        except UnicodeEncodeError:
            record.msg = record.msg.encode("ascii", "replace").decode()
            super().emit(record)


def setup_logger(name="trading_bot", log_file="logs/trading.log", level=None):
    """Configure and return a logger with file and console handlers.

    The logger's level gates ALL records before they reach any handler. If
    this is set to INFO (the old hardcoded default), every `log.debug(...)`
    call is silently dropped — which means the file handler's "DEBUG level"
    setting below was a no-op, hiding diagnostic info during the 2026-05-16
    crypto-bar-fetch debugging. Now respects a `LOG_LEVEL` env var so
    verbosity can be cranked without a code change (`LOG_LEVEL=DEBUG`
    surfaces every `log.debug` to the file; console stays at INFO).
    """
    if level is None:
        level = os.getenv("LOG_LEVEL", "INFO")
    logger = logging.getLogger(name)
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if logger.handlers:
        return logger

    # Create logs directory
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Format
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )

    # Console handler — safe against Windows cp1252 encoding errors
    console = SafeStreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File handler with rotation (UTF-8 to handle emoji/unicode in notifications)
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=10,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Trade-specific log
    trade_log = Path(log_file).parent / "trades.log"
    trade_handler = logging.handlers.RotatingFileHandler(
        str(trade_log),
        maxBytes=10 * 1024 * 1024,
        backupCount=20,
        encoding='utf-8'
    )
    trade_handler.setLevel(logging.INFO)
    trade_handler.setFormatter(fmt)
    trade_logger = logging.getLogger(f"{name}.trades")
    trade_logger.addHandler(trade_handler)

    return logger


def get_logger(name):
    """Get a child logger."""
    return logging.getLogger(f"trading_bot.{name}")
