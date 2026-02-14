# Algo Trading Bot - Setup Guide

## Quick Start (5 Minutes)

### 1. Install Dependencies
```bash
pip install -r requirements.txt
```

### 2. Configure Environment
```bash
cp .env.example .env
# Edit .env with your credentials
```

### 3. Start Paper Trading (NO real money)
```bash
python run.py paper
```

### 4. Run Backtest First (Recommended)
```bash
python run.py backtest
python run.py backtest momentum
```

---

## Detailed Setup

### IBKR (Interactive Brokers)

1. **Download TWS or IB Gateway** from ibkr.com
2. **Open TWS** and log in (paper account first!)
3. **Enable API**: Edit > Global Configuration > API > Settings
   - Check "Enable ActiveX and Socket Clients"
   - Uncheck "Read-Only API"
   - Set Socket Port: **7497** (paper) or **7496** (live)
   - Add `127.0.0.1` to trusted IPs
4. **Update .env**:
   ```
   TRADING_MODE=paper
   IBKR_HOST=127.0.0.1
   IBKR_PORT=7497
   IBKR_CLIENT_ID=1
   ```

### TradersPost

1. **Create account** at traderspost.io
2. **Create a strategy** in TradersPost
3. **Get webhook URL** from strategy settings
4. **Update .env**:
   ```
   TRADERSPOST_WEBHOOK_URL=https://traderspost.io/api/v1/webhook/YOUR_ID
   TRADERSPOST_API_KEY=your_key
   ```

### TradingView Alerts

1. **Open TradingView** and add the Pine Script from `tradingview_alerts.pine`
2. **Create an alert** on the strategy
3. **Set webhook URL**: `http://YOUR_SERVER_IP:5001/webhook/tradingview`
4. **Set alert message** (copy from Pine Script comments)
5. **Update .env**:
   ```
   TRADINGVIEW_WEBHOOK_SECRET=your_secret_here
   ```

### Discord Notifications (Optional)

1. Create a Discord server or use existing
2. Create a webhook: Server Settings > Integrations > Webhooks
3. Copy webhook URL to .env:
   ```
   DISCORD_WEBHOOK_URL=https://discord.com/api/webhooks/YOUR_WEBHOOK
   ```

---

## Usage Commands

```bash
# Paper trading (default - START HERE)
python run.py paper

# Live trading (requires CONFIRM)
python run.py live

# Backtest strategies
python run.py backtest                                    # Default mean_reversion
python run.py backtest momentum                           # Momentum strategy
python -m bot.main --backtest --strategy vwap_scalp       # VWAP strategy
python -m bot.main --backtest --strategy mean_reversion --symbols SPY QQQ AAPL

# Full backtest with dates
python -m bot.main --backtest --strategy momentum --start 2024-06-01 --end 2024-12-31

# Start with custom capital
python -m bot.main --mode paper --capital 10000

# Dashboard only (no trading)
python -m bot.main --dashboard
```

---

## Architecture

```
Signal Flow:
TradingView Alert ──┐
                    ├──> Risk Manager ──> IBKR Order Execution
Strategy Engine ────┘         │
                              ├──> TradersPost Webhook
                              └──> Discord Notification

Data Flow:
IBKR Real-time Data ──> Market Data Feed ──> Strategies
yfinance (fallback) ──┘                      │
                                             ├── Mean Reversion
                                             ├── Momentum
                                             ├── VWAP Scalping
                                             └── Pairs Trading
```

---

## Risk Management

| Parameter | $5K Account | $10K | $25K | $50K+ |
|-----------|-------------|------|------|-------|
| Max Positions | 5 | 8 | 12 | 15 |
| Risk Per Trade | 1% ($50) | 1.5% ($150) | 1.5% ($375) | 2% ($1000) |
| Max Position | 15% ($750) | 12% ($1200) | 10% ($2500) | 8% ($4000) |
| Daily Loss Limit | 2% ($100) | 2% ($200) | 2% ($500) | 2% ($1000) |
| Max Drawdown | 10% ($500) | 10% | 10% | 10% |

**The bot auto-scales** as your account grows. Hit $10K and it automatically
increases to 8 max positions.

---

## Strategy Overview

### Mean Reversion (30% allocation)
- Buys oversold stocks (RSI < 30 + Bollinger Band touch)
- Targets mean reversion to SMA
- Best in range-bound markets

### Momentum (30% allocation)
- Rides strong trends (EMA crossover + ADX > 25)
- Volume surge confirmation
- ATR-based stops and targets (2:1 R/R minimum)

### VWAP Scalping (20% allocation)
- Trades around VWAP (institutional reference price)
- Quick in-and-out scalps (max 30 min hold)
- Max 6 trades per day to prevent overtrading

### Pairs Trading (20% allocation)
- Market-neutral statistical arbitrage
- Trades correlated stock pairs (AAPL/MSFT, AMD/NVDA, etc.)
- Profits when spread reverts regardless of market direction

---

## Dashboard

Access at `http://localhost:5000` when bot is running.

Shows:
- Live balance and P&L
- Open positions with stops/targets
- Trade history
- Drawdown monitoring
- System notifications

---

## Important Notes

1. **ALWAYS start with paper trading** - never go live until you've tested
2. **Backtest first** - validate strategies on historical data
3. **PDT Rule** - Under $25K you're limited to 3 day trades per 5 days
4. **The bot respects this** - VWAP scalps count as day trades
5. **Commission costs** - IBKR Pro charges ~$0.005/share
6. **Market hours only** - Bot only trades 9:35 AM - 3:45 PM ET
