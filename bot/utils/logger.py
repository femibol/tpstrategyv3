"""
Logging setup for the trading bot.
"""
import logging
import logging.handlers
from pathlib import Path


def setup_logger(name="trading_bot", log_file="logs/trading.log", level="INFO"):
    """Configure and return a logger with file and console handlers."""
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

    # Console handler
    console = logging.StreamHandler()
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File handler with rotation
    file_handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=50 * 1024 * 1024,  # 50MB
        backupCount=10
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(fmt)
    logger.addHandler(file_handler)

    # Trade-specific log
    trade_log = Path(log_file).parent / "trades.log"
    trade_handler = logging.handlers.RotatingFileHandler(
        str(trade_log),
        maxBytes=10 * 1024 * 1024,
        backupCount=20
    )
    trade_handler.setLevel(logging.INFO)
    trade_handler.setFormatter(fmt)
    trade_logger = logging.getLogger(f"{name}.trades")
    trade_logger.addHandler(trade_handler)

    return logger


def get_logger(name):
    """Get a child logger."""
    return logging.getLogger(f"trading_bot.{name}")
