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
    def traderspost_webhook_url_secondary(self):
        return os.getenv("TRADERSPOST_WEBHOOK_URL_SECONDARY", "")

    @property
    def traderspost_webhook_url_crypto(self):
        return os.getenv("TRADERSPOST_WEBHOOK_URL_CRYPTO", "")

    @property
    def traderspost_api_key(self):
        return os.getenv("TRADERSPOST_API_KEY", "")

    # --- TradingView ---
    @property
    def tradingview_webhook_secret(self):
        return os.getenv("TRADINGVIEW_WEBHOOK_SECRET", "")

    # --- Alpaca Markets ---
    @property
    def alpaca_api_key(self):
        return os.getenv("ALPACA_API_KEY", "")

    @property
    def alpaca_secret_key(self):
        return os.getenv("ALPACA_SECRET_KEY", "")

    @property
    def alpaca_base_url(self):
        return os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")

    # --- Politician Tracker ---
    @property
    def capitoltrades_api_key(self):
        return os.getenv("CAPITOLTRADES_API_KEY", "")

    # --- News API ---
    @property
    def news_api_key(self):
        return os.getenv("NEWS_API_KEY", "")

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

    # --- Trading Universe ---
    def get_universe(self):
        """Load full trading universe from universe.yaml.
        Returns flat deduplicated list of all symbols across all sectors."""
        universe_data = self._load_yaml("universe.yaml")
        if not universe_data:
            return []
        all_symbols = []
        for section, symbols in universe_data.items():
            if isinstance(symbols, list):
                all_symbols.extend(s for s in symbols if isinstance(s, str))
        # Deduplicate preserving order
        seen = set()
        unique = []
        for s in all_symbols:
            s_upper = s.upper()
            if s_upper not in seen:
                seen.add(s_upper)
                unique.append(s)
        return unique

    # --- Trading Mode Profiles ---
    TRADING_PROFILES = {
        "scalp": {
            "label": "Scalp",
            "description": "Quick trades, tight stops, intraday only",
            "risk": {
                "stop_loss_pct": 0.015,
                "trailing_stop_pct": 0.01,
                "take_profit_pct": 0.03,
                "max_positions": 6,
                "risk_per_trade_pct": 0.005,
            },
            "schedule": {
                "avoid_first_minutes": 30,
                "avoid_last_minutes": 15,
                "overnight": {"enabled": False},
                "premarket": {"enabled": False},
            },
            "preferred_strategies": ["vwap_scalp", "mean_reversion"],
        },
        "swing": {
            "label": "Swing",
            "description": "Multi-day holds, medium stops, trend following",
            "risk": {
                "stop_loss_pct": 0.04,
                "trailing_stop_pct": 0.025,
                "take_profit_pct": 0.08,
                "max_positions": 5,
                "risk_per_trade_pct": 0.01,
            },
            "schedule": {
                "avoid_first_minutes": 30,
                "avoid_last_minutes": 30,
                "overnight": {"enabled": True, "min_profit_pct": 0.01, "require_uptrend": True},
                "premarket": {"enabled": False},
            },
            "preferred_strategies": ["momentum", "smc_forever", "pairs_trading"],
        },
        "invest": {
            "label": "Invest",
            "description": "Longer holds, wide stops, follow smart money",
            "risk": {
                "stop_loss_pct": 0.08,
                "trailing_stop_pct": 0.05,
                "take_profit_pct": 0.20,
                "max_positions": 8,
                "risk_per_trade_pct": 0.015,
            },
            "schedule": {
                "avoid_first_minutes": 30,
                "avoid_last_minutes": 15,
                "overnight": {"enabled": True, "min_profit_pct": 0.005, "require_uptrend": False},
                "premarket": {"enabled": True},
            },
            "preferred_strategies": ["momentum", "smc_forever"],
        },
    }

    @property
    def trading_profile(self):
        return self.settings.get("trading_profile", "swing")

    def apply_profile(self, profile_name):
        """Apply a trading mode profile, updating settings in memory."""
        profile = self.TRADING_PROFILES.get(profile_name)
        if not profile:
            return False
        # Update risk settings
        risk = self.settings.setdefault("risk", {})
        risk.update(profile["risk"])
        # Update schedule settings
        schedule = self.settings.setdefault("schedule", {})
        for key, val in profile["schedule"].items():
            if isinstance(val, dict):
                schedule.setdefault(key, {}).update(val)
            else:
                schedule[key] = val
        self.settings["trading_profile"] = profile_name
        return True

    def save_settings(self):
        """Persist current settings back to settings.yaml."""
        filepath = self.config_dir / "settings.yaml"
        with open(filepath, "w") as f:
            yaml.dump(self.settings, f, default_flow_style=False, sort_keys=False)

    def update_setting(self, path, value):
        """Update a nested setting by dot-path (e.g. 'risk.stop_loss_pct')."""
        keys = path.split(".")
        d = self.settings
        for key in keys[:-1]:
            d = d.setdefault(key, {})
        # Try to cast to appropriate type
        if isinstance(value, str):
            if value.lower() in ("true", "false"):
                value = value.lower() == "true"
            else:
                try:
                    value = int(value)
                except ValueError:
                    try:
                        value = float(value)
                    except ValueError:
                        pass
        d[keys[-1]] = value
