"""
Technical Indicators - calculation library used by strategies.
Pure numpy implementations for speed.
"""
import numpy as np
from bot.utils.logger import get_logger

log = get_logger("data.indicators")


class TechnicalIndicators:
    """
    Technical indicator calculations.
    All methods accept numpy arrays and return values.
    """

    @staticmethod
    def sma(data, period):
        """Simple Moving Average."""
        if len(data) < period:
            return None
        return np.mean(data[-period:])

    @staticmethod
    def ema(data, period):
        """Exponential Moving Average - returns full array."""
        if len(data) < period:
            return None

        multiplier = 2 / (period + 1)
        ema_values = np.zeros(len(data))
        ema_values[period - 1] = np.mean(data[:period])

        for i in range(period, len(data)):
            ema_values[i] = (data[i] * multiplier) + (ema_values[i - 1] * (1 - multiplier))

        return ema_values

    @staticmethod
    def rsi(data, period=14):
        """Relative Strength Index."""
        if len(data) < period + 1:
            return 50  # neutral default

        deltas = np.diff(data)
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)

        avg_gain = np.mean(gains[-period:])
        avg_loss = np.mean(losses[-period:])

        if avg_loss == 0:
            return 100.0

        rs = avg_gain / avg_loss
        rsi = 100 - (100 / (1 + rs))
        return float(rsi)

    @staticmethod
    def atr(highs, lows, closes, period=14):
        """Average True Range - volatility measure."""
        if len(highs) < period + 1:
            return None

        tr_values = []
        for i in range(1, len(highs)):
            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            tr_values.append(tr)

        if len(tr_values) < period:
            return None

        return float(np.mean(tr_values[-period:]))

    @staticmethod
    def adx(highs, lows, closes, period=14):
        """Average Directional Index - trend strength."""
        if len(highs) < period * 2:
            return None

        plus_dm = []
        minus_dm = []
        tr_values = []

        for i in range(1, len(highs)):
            up_move = highs[i] - highs[i - 1]
            down_move = lows[i - 1] - lows[i]

            plus_dm.append(up_move if up_move > down_move and up_move > 0 else 0)
            minus_dm.append(down_move if down_move > up_move and down_move > 0 else 0)

            tr = max(
                highs[i] - lows[i],
                abs(highs[i] - closes[i - 1]),
                abs(lows[i] - closes[i - 1])
            )
            tr_values.append(tr)

        if len(tr_values) < period:
            return None

        # Smoothed values
        atr = np.mean(tr_values[-period:])
        plus_di = (np.mean(plus_dm[-period:]) / atr * 100) if atr > 0 else 0
        minus_di = (np.mean(minus_dm[-period:]) / atr * 100) if atr > 0 else 0

        di_sum = plus_di + minus_di
        if di_sum == 0:
            return 0

        dx = abs(plus_di - minus_di) / di_sum * 100
        return float(dx)

    @staticmethod
    def bollinger_bands(data, period=20, std_dev=2.0):
        """Bollinger Bands - returns (upper, middle, lower)."""
        if len(data) < period:
            return None, None, None

        middle = np.mean(data[-period:])
        std = np.std(data[-period:])

        upper = middle + (std_dev * std)
        lower = middle - (std_dev * std)

        return float(upper), float(middle), float(lower)

    @staticmethod
    def vwap(highs, lows, closes, volumes):
        """Volume Weighted Average Price (intraday)."""
        typical_price = (highs + lows + closes) / 3
        cumulative_tp_vol = np.cumsum(typical_price * volumes)
        cumulative_vol = np.cumsum(volumes)

        vwap = cumulative_tp_vol / np.where(cumulative_vol > 0, cumulative_vol, 1)
        return vwap

    @staticmethod
    def macd(data, fast=12, slow=26, signal=9):
        """MACD - returns (macd_line, signal_line, histogram)."""
        if len(data) < slow + signal:
            return None, None, None

        fast_ema = TechnicalIndicators.ema(data, fast)
        slow_ema = TechnicalIndicators.ema(data, slow)

        if fast_ema is None or slow_ema is None:
            return None, None, None

        macd_line = fast_ema - slow_ema
        signal_line = TechnicalIndicators.ema(macd_line[slow - 1:], signal)

        if signal_line is None:
            return None, None, None

        # Pad signal line to match length
        padded_signal = np.zeros(len(macd_line))
        padded_signal[-len(signal_line):] = signal_line[-len(signal_line):]

        histogram = macd_line - padded_signal

        return macd_line, padded_signal, histogram

    @staticmethod
    def zscore(data, period=20):
        """Z-Score of the latest value."""
        if len(data) < period:
            return 0

        window = data[-period:]
        mean = np.mean(window)
        std = np.std(window)

        if std == 0:
            return 0

        return float((data[-1] - mean) / std)

    @staticmethod
    def stochastic(highs, lows, closes, k_period=14, d_period=3):
        """Stochastic oscillator - returns (%K, %D)."""
        if len(closes) < k_period:
            return None, None

        highest = np.max(highs[-k_period:])
        lowest = np.min(lows[-k_period:])

        if highest == lowest:
            return 50.0, 50.0

        k = ((closes[-1] - lowest) / (highest - lowest)) * 100

        # %D is SMA of %K (simplified)
        d = k  # Would need history for proper %D

        return float(k), float(d)
