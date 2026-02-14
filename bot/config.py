"""
Configuration loader - reads settings.yaml, strategies.yaml, and .env
"""
import os
import yaml
from pathlib import Path
from dotenv import load_dotenv


class Config:
    """Central configuration for the trading bot."""

    def __init__(self, config_dir=None):
        self.base_dir = Path(__file__).parent.parent
        self.config_dir = Path(config_dir) if config_dir else self.base_dir / "config"

        # Load .env file
        env_path = self.base_dir / ".env"
        if env_path.exists():
            load_dotenv(env_path)
        else:
            load_dotenv(self.base_dir / ".env.example")

        # Load YAML configs
        self.settings = self._load_yaml("settings.yaml")
        self.strategies = self._load_yaml("strategies.yaml")

        # Trading mode
        self.mode = os.getenv("TRADING_MODE", "paper")
        self.is_live = self.mode == "live"
        self.is_paper = self.mode == "paper"

    def _load_yaml(self, filename):
        filepath = self.config_dir / filename
        if filepath.exists():
            with open(filepath, "r") as f:
                return yaml.safe_load(f)
        return {}

    # --- Capital ---
    @property
    def starting_balance(self):
        return self.settings.get("capital", {}).get("starting_balance", 5000)

    @property
    def max_daily_loss(self):
        return self.settings.get("capital", {}).get("max_daily_loss", 0.02)

    @property
    def max_drawdown(self):
        return self.settings.get("capital", {}).get("max_drawdown", 0.10)

    @property
    def reserve_cash_pct(self):
        return self.settings.get("capital", {}).get("reserve_cash_pct", 0.20)

    # --- Risk ---
    @property
    def risk_config(self):
        return self.settings.get("risk", {})

    @property
    def max_positions(self):
        return self.risk_config.get("max_positions", 5)

    @property
    def risk_per_trade(self):
        return self.risk_config.get("risk_per_trade_pct", 0.01)

    @property
    def stop_loss_pct(self):
        return self.risk_config.get("stop_loss_pct", 0.03)

    @property
    def take_profit_pct(self):
        return self.risk_config.get("take_profit_pct", 0.06)

    # --- Schedule ---
    @property
    def schedule_config(self):
        return self.settings.get("schedule", {})

    @property
    def timezone(self):
        return self.schedule_config.get("timezone", "US/Eastern")

    # --- Scaling ---
    def get_scaling_tier(self, current_balance):
        """Get risk parameters based on current balance tier."""
        scaling = self.settings.get("scaling", {})
        if not scaling.get("enabled", False):
            return None

        tiers = scaling.get("tiers", [])
        active_tier = None
        for tier in tiers:
            if current_balance >= tier["min_balance"]:
                active_tier = tier
        return active_tier

    # --- IBKR ---
    @property
    def ibkr_host(self):
        return os.getenv("IBKR_HOST", "127.0.0.1")

    @property
    def ibkr_port(self):
        return int(os.getenv("IBKR_PORT", "7497"))

    @property
    def ibkr_client_id(self):
        return int(os.getenv("IBKR_CLIENT_ID", "1"))

    # --- TradersPost ---
    @property
    def traderspost_webhook_url(self):
        return os.getenv("TRADERSPOST_WEBHOOK_URL", "")

    @property
    def traderspost_api_key(self):
        return os.getenv("TRADERSPOST_API_KEY", "")

    # --- TradingView ---
    @property
    def tradingview_webhook_secret(self):
        return os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "")

    # --- Notifications ---
    @property
    def discord_webhook_url(self):
        return os.getenv("DISCORD_WEBHOOK_URL", "")

    # --- Dashboard ---
    @property
    def dashboard_host(self):
        return os.getenv("DASHBOARD_HOST", "0.0.0.0")

    @property
    def dashboard_port(self):
        return int(os.getenv("DASHBOARD_PORT", "5000"))

    # --- Strategy configs ---
    @property
    def strategy_allocation(self):
        return self.strategies.get("allocation", {})

    def get_strategy_config(self, strategy_name):
        return self.strategies.get(strategy_name, {})
