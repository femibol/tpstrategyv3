"""
SMC Forever Model Strategy (ICT / Smart Money Concepts)

The highest-conviction model from the prop firm playbook:
1. Liquidity Sweep + Stop Hunt (context)
2. SMT Divergence (confirmation)
3. CISD + Displacement (shift in control)
4. FVG / iFVG Entry (defined risk entry)
5. Draw on Liquidity target (known target)

Trades 1-3x per week. Every condition must align = A+ setups only.
"""
import numpy as np
from bot.strategies.base import BaseStrategy
from bot.utils.logger import get_logger

log = get_logger("strategy.smc_forever")


class SMCForeverStrategy(BaseStrategy):
    """
    Smart Money Concepts - "The Forever Model"

    Stacks context + confirmation + execution:
    - Liquidity sweep (stop hunt clears the way)
    - SMT divergence (smart money confirmation)
    - CISD (structural shift in delivery)
    - FVG entry (precise risk, clean invalidation)
    - DOL target (draw on liquidity = known target)

    Selective: skips when conditions don't fully align.
    """

    def __init__(self, config, indicators, capital):
        super().__init__(config, indicators, capital)
        self.swing_lookback = config.get("swing_lookback", 5)
        self.fvg_min_size_pct = config.get("fvg_min_size_pct", 0.001)
        self.smt_lookback = config.get("smt_lookback", 10)
        self.smt_pairs = config.get("smt_pairs", [["SPY", "QQQ"]])
        self.displacement_atr_mult = config.get("displacement_atr_mult", 1.5)
        self.risk_reward_min = config.get("risk_reward_min", 2.0)
        self.max_trades_per_week = config.get("max_trades_per_week", 3)
        self.stop_buffer_pct = config.get("stop_buffer_pct", 0.002)
        self.trades_this_week = 0
        self._week_number = None

        # SMT cache: store correlated pair data for divergence checks
        self._smt_cache = {}

    def generate_signals(self, market_data):
        """Run the Forever Model scan on all symbols."""
        signals = []

        # Reset weekly trade counter
        from datetime import datetime
        import pytz
        now = datetime.now(pytz.timezone("US/Eastern"))
        current_week = now.isocalendar()[1]
        if self._week_number != current_week:
            self._week_number = current_week
            self.trades_this_week = 0

        if self.trades_this_week >= self.max_trades_per_week:
            for symbol in self.symbols:
                self.scan_results[symbol] = {
                    "verdict": "WEEKLY LIMIT",
                    "trades_this_week": self.trades_this_week,
                    "max_per_week": self.max_trades_per_week,
                }
            return signals

        # Pre-fetch SMT pair data
        self._load_smt_data(market_data)

        for symbol in self.symbols:
            try:
                sig = self._analyze_symbol(symbol, market_data)
                if sig:
                    signals.append(sig)
                    self.trades_this_week += 1
                    if self.trades_this_week >= self.max_trades_per_week:
                        break
            except Exception as e:
                log.debug(f"Error analyzing {symbol}: {e}")
                self.scan_results[symbol] = {"status": "error", "verdict": "ERROR", "detail": str(e)}

        return signals

    def _load_smt_data(self, market_data):
        """Pre-fetch data for SMT divergence pairs."""
        self._smt_cache = {}
        for pair in self.smt_pairs:
            if len(pair) != 2:
                continue
            sym_a, sym_b = pair
            bars_a = market_data.get_bars(sym_a, 80)
            bars_b = market_data.get_bars(sym_b, 80)
            if bars_a is not None and bars_b is not None and len(bars_a) > 20 and len(bars_b) > 20:
                self._smt_cache[f"{sym_a}/{sym_b}"] = {
                    "highs_a": bars_a["high"].values,
                    "lows_a": bars_a["low"].values,
                    "highs_b": bars_b["high"].values,
                    "lows_b": bars_b["low"].values,
                }

    def _get_smt_for_symbol(self, symbol):
        """Find SMT divergence relevant to this symbol."""
        for pair in self.smt_pairs:
            if symbol in pair:
                key = f"{pair[0]}/{pair[1]}"
                data = self._smt_cache.get(key)
                if data:
                    return self.indicators.detect_smt_divergence(
                        data["lows_a"], data["highs_a"],
                        data["lows_b"], data["highs_b"],
                        lookback=self.smt_lookback
                    ), key
        return None, None

    def _analyze_symbol(self, symbol, market_data):
        """
        Full Forever Model analysis on a single symbol.

        Must pass ALL 5 conditions for A+ setup:
        1. Liquidity sweep detected
        2. SMT divergence confirmed
        3. CISD + displacement present
        4. FVG entry zone available
        5. Clear draw on liquidity (target)
        """
        bars = market_data.get_bars(symbol, 80)
        if bars is None or len(bars) < 40:
            self.scan_results[symbol] = {"status": "no_data", "verdict": "WAIT"}
            return None

        opens = bars["open"].values
        highs = bars["high"].values
        lows = bars["low"].values
        closes = bars["close"].values
        current_price = closes[-1]

        # === 1. SWING POINTS & LIQUIDITY LEVELS ===
        swing_highs, swing_lows = self.indicators.find_swing_points(
            highs, lows, lookback=self.swing_lookback
        )

        if not swing_highs or not swing_lows:
            self.scan_results[symbol] = {
                "price": round(current_price, 2),
                "verdict": "NO STRUCTURE",
                "swing_highs": 0,
                "swing_lows": 0,
            }
            return None

        # Nearest liquidity levels
        nearest_high = max(swing_highs, key=lambda x: x[1])
        nearest_low = min(swing_lows, key=lambda x: x[1])
        buy_side_liq = nearest_high[1]
        sell_side_liq = nearest_low[1]

        # === 2. LIQUIDITY SWEEP ===
        sweeps = self.indicators.detect_liquidity_sweep(
            highs, lows, closes, swing_highs, swing_lows
        )

        bullish_sweeps = [s for s in sweeps if s["type"] == "bullish"]
        bearish_sweeps = [s for s in sweeps if s["type"] == "bearish"]
        has_sweep = len(sweeps) > 0
        sweep_type = None
        if bullish_sweeps:
            sweep_type = "bullish"
        elif bearish_sweeps:
            sweep_type = "bearish"

        # === 3. SMT DIVERGENCE ===
        smt, smt_pair_key = self._get_smt_for_symbol(symbol)
        has_smt = smt is not None

        # === 4. ATR for displacement/stops ===
        atr = self.indicators.atr(highs, lows, closes, period=14)

        # === 5. CISD (Change in State of Delivery) ===
        cisd = self.indicators.detect_cisd(opens, highs, lows, closes)
        has_cisd = cisd is not None

        # === 6. DISPLACEMENT ===
        displacements = self.indicators.detect_displacement(
            opens, closes, atr, min_body_atr=self.displacement_atr_mult
        )
        has_displacement = len(displacements) > 0

        # === 7. FVG DETECTION ===
        fvgs = self.indicators.detect_fvg(highs, lows, min_size_pct=self.fvg_min_size_pct)

        # Find unfilled FVGs near current price (within 2% range)
        active_fvgs = []
        for fvg in fvgs[-15:]:  # Check recent FVGs
            dist = abs(current_price - fvg["mid"]) / current_price
            if dist < 0.02:
                active_fvgs.append(fvg)

        bullish_fvgs = [f for f in active_fvgs if f["type"] == "bullish"]
        bearish_fvgs = [f for f in active_fvgs if f["type"] == "bearish"]
        has_fvg = len(active_fvgs) > 0

        # === BUILD SCAN RESULT ===
        checks = {
            "sweep": has_sweep,
            "smt": has_smt,
            "cisd": has_cisd,
            "displacement": has_displacement,
            "fvg": has_fvg,
        }
        passed = sum(1 for v in checks.values() if v)

        # Determine bias alignment
        bullish_aligned = (
            sweep_type == "bullish"
            and (not has_smt or smt["type"] == "bullish")
            and (not has_cisd or cisd["type"] == "bullish")
            and len(bullish_fvgs) > 0
        )
        bearish_aligned = (
            sweep_type == "bearish"
            and (not has_smt or smt["type"] == "bearish")
            and (not has_cisd or cisd["type"] == "bearish")
            and len(bearish_fvgs) > 0
        )

        # Verdict
        if passed >= 4 and (bullish_aligned or bearish_aligned):
            verdict = "A+ SETUP"
        elif passed >= 3 and has_sweep:
            verdict = "BUILDING"
        elif passed >= 2:
            verdict = "WATCHING"
        elif has_sweep:
            verdict = "SWEEP ONLY"
        else:
            verdict = "NO SETUP"

        self.scan_results[symbol] = {
            "price": round(current_price, 2),
            "buy_side_liq": round(buy_side_liq, 2),
            "sell_side_liq": round(sell_side_liq, 2),
            "sweep": sweep_type or "none",
            "sweep_count": len(sweeps),
            "smt": smt["type"] if smt else "none",
            "smt_pair": smt_pair_key or "N/A",
            "smt_desc": smt["desc"] if smt else "N/A",
            "cisd": cisd["type"] if cisd else "none",
            "cisd_level": round(cisd["shift_level"], 2) if cisd else 0,
            "displacement": len(displacements),
            "disp_type": displacements[-1]["type"] if displacements else "none",
            "fvg_count": len(active_fvgs),
            "fvg_bull": len(bullish_fvgs),
            "fvg_bear": len(bearish_fvgs),
            "nearest_fvg": round(active_fvgs[-1]["mid"], 2) if active_fvgs else 0,
            "atr": round(atr, 2) if atr else 0,
            "checks": checks,
            "checks_passed": passed,
            "verdict": verdict,
        }

        # === SIGNAL GENERATION (A+ setups only) ===
        if passed < 4 or not (bullish_aligned or bearish_aligned):
            return None

        if bullish_aligned and bullish_fvgs:
            # LONG setup
            entry_fvg = bullish_fvgs[-1]  # Most recent bullish FVG
            entry_price = entry_fvg["mid"]  # Enter at FVG midpoint
            stop_loss = entry_fvg["bottom"] * (1 - self.stop_buffer_pct)
            target = buy_side_liq  # Draw on liquidity = buy-side

            # Check R:R
            risk = abs(entry_price - stop_loss)
            reward = abs(target - entry_price)
            if risk <= 0 or reward / risk < self.risk_reward_min:
                return None

            confidence = min(1.0, 0.5 + (passed - 3) * 0.15)
            if has_smt:
                confidence = min(1.0, confidence + 0.1)

            reasons = []
            if has_sweep:
                reasons.append(f"Sweep@${sweeps[-1]['level']:.2f}")
            if has_smt:
                reasons.append(f"SMT({smt['type']})")
            if has_cisd:
                reasons.append(f"CISD({cisd['candles_broken']}bars)")
            if has_displacement:
                reasons.append(f"Disp({displacements[-1]['body_atr_ratio']}xATR)")
            reasons.append(f"FVG@${entry_fvg['mid']:.2f}")
            reasons.append(f"DOL@${target:.2f}")

            signal = {
                "symbol": symbol,
                "action": "buy",
                "price": current_price,
                "stop_loss": stop_loss,
                "take_profit": target,
                "confidence": confidence,
                "reason": f"Forever Model LONG: {', '.join(reasons)}",
                "max_hold_bars": 80,
                "bar_seconds": self._timeframe_to_seconds(),
                "trailing_stop_pct": atr / current_price if atr else 0.02,
            }

            log.info(f"A+ SIGNAL: {signal['reason']} | {symbol} @ ${current_price:.2f} | R:R={reward/risk:.1f}")
            self.signals_generated += 1
            return signal

        elif bearish_aligned and bearish_fvgs:
            # SHORT setup
            entry_fvg = bearish_fvgs[-1]
            entry_price = entry_fvg["mid"]
            stop_loss = entry_fvg["top"] * (1 + self.stop_buffer_pct)
            target = sell_side_liq

            risk = abs(stop_loss - entry_price)
            reward = abs(entry_price - target)
            if risk <= 0 or reward / risk < self.risk_reward_min:
                return None

            confidence = min(1.0, 0.5 + (passed - 3) * 0.15)
            if has_smt:
                confidence = min(1.0, confidence + 0.1)

            reasons = []
            if has_sweep:
                reasons.append(f"Sweep@${sweeps[-1]['level']:.2f}")
            if has_smt:
                reasons.append(f"SMT({smt['type']})")
            if has_cisd:
                reasons.append(f"CISD({cisd['candles_broken']}bars)")
            if has_displacement:
                reasons.append(f"Disp({displacements[-1]['body_atr_ratio']}xATR)")
            reasons.append(f"FVG@${entry_fvg['mid']:.2f}")
            reasons.append(f"DOL@${target:.2f}")

            signal = {
                "symbol": symbol,
                "action": "sell",
                "price": current_price,
                "stop_loss": stop_loss,
                "take_profit": target,
                "confidence": confidence,
                "reason": f"Forever Model SHORT: {', '.join(reasons)}",
                "max_hold_bars": 80,
                "bar_seconds": self._timeframe_to_seconds(),
                "trailing_stop_pct": atr / current_price if atr else 0.02,
            }

            log.info(f"A+ SIGNAL: {signal['reason']} | {symbol} @ ${current_price:.2f} | R:R={reward/risk:.1f}")
            self.signals_generated += 1
            return signal

        return None

    def _timeframe_to_seconds(self):
        tf = self.timeframe
        if "m" in tf:
            return int(tf.replace("m", "")) * 60
        elif "h" in tf:
            return int(tf.replace("h", "")) * 3600
        return 900
