"""Transaction-cost gate for entry signals.

Every signal carries an *optimistic* expected edge (its `take_profit`
relative to its entry `price`). A strategy that wins +30 bps on average
but pays 35 bps in fees + spread is a guaranteed loser, but nothing in
the current pipeline subtracts those costs before accepting the trade.

This module estimates a conservative round-trip cost (fee + spread × a
multiplier; impact is omitted at retail scale) and rejects signals whose
expected edge doesn't clear the cost by a configurable safety ratio
(default 2×). It's ratio-based on purpose: a $1 penny stock with a $0.05
spread (~500 bps cost) sounds expensive in absolute bps but trivially
clears when the strategy targets a +12% move (1200 bps edge → ratio 2.4×).
A fixed-bps cost cap would over-kill that case, which is exactly the
cheap-stock momentum scalping the user runs.

Wired into `RiskManager._check_all_rules` as an early-exit gate so
rejected signals never reach position sizing or execution.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)


class CostModel:
    """Estimates round-trip transaction cost and gates entries by edge:cost.

    Costs in bps round-trip (entry + exit combined):
      - fee:    broker commission / taker fee
      - spread: configured default × multiplier (captures cross-spread + slip)

    A live spread can be passed in to override the default; impact term is
    intentionally omitted — at single-account retail position sizes
    (~$1-2k notional) impact on liquid names is sub-bp and dominated by
    spread.
    """

    def __init__(self, config):
        cfg = (config.settings.get("cost_model", {}) or {})
        self.enabled = bool(cfg.get("enabled", True))
        # IBKR Pro tiered runs ~0.5-1 bp on liquid names but the $1 minimum
        # bites on small share counts; 2 bp is a defensible round-trip
        # average for the order sizes this bot runs.
        self.equity_fee_bps = float(cfg.get("equity_fee_bps", 2.0))
        # TradersPost → Alpaca crypto taker is ~15 bp × 2 sides = 30 bp.
        self.crypto_fee_bps = float(cfg.get("crypto_fee_bps", 30.0))
        # Default cross-spread when we don't have a live quote. These are
        # one-side bps; round-trip multiplies by 2 via spread_mult.
        self.equity_spread_bps_default = float(cfg.get("equity_spread_bps_default", 5.0))
        self.crypto_spread_bps_default = float(cfg.get("crypto_spread_bps_default", 10.0))
        # Spread multiplier — paper-account fills routinely beat live;
        # 2.0× accounts for both sides plus typical slippage past mid.
        self.spread_mult = float(cfg.get("spread_mult", 2.0))
        # Edge:cost ratio. 2× means the take_profit must cover at least
        # twice the round-trip friction. Conservative; tune downward for
        # high-frequency, upward for low-frequency.
        self.min_edge_cost_ratio = float(cfg.get("min_edge_cost_ratio", 2.0))

    def round_trip_cost_bps(self, asset_class, live_spread_bps=None):
        """Total round-trip cost in bps (entry + exit, fee + spread×mult)."""
        if asset_class == "crypto":
            fee = self.crypto_fee_bps
            base_spread = (live_spread_bps if live_spread_bps is not None
                           else self.crypto_spread_bps_default)
        else:
            fee = self.equity_fee_bps
            base_spread = (live_spread_bps if live_spread_bps is not None
                           else self.equity_spread_bps_default)
        return fee + base_spread * self.spread_mult

    def expected_edge_bps(self, signal):
        """Optimistic per-trade edge in bps.

        Takes the MAX of two measures:
          - tp_edge: stated take_profit minus entry, in bps
          - sl_edge: stop distance × 2, in bps (proxy for post-stretch target)

        Why both: the engine's R/R STRETCH path raises any under-2:1 target
        to 2× the stop distance AFTER RiskManager runs. The pre-stretch
        take_profit reaching this gate routinely understates the actual
        ask — e.g. ATOM signal had tp $2.06 (100 bp) but stretched to
        $2.25 (1000 bp). Using max() lets the gate see the post-stretch
        reality without coupling to engine internals.

        Returns 0 if price is missing or non-positive (fails closed).
        """
        price = float(signal.get("price", 0) or 0)
        if price <= 0:
            return 0.0
        tp = float(signal.get("take_profit", 0) or 0)
        tp_edge_bps = abs(tp - price) / price * 10000.0 if tp > 0 else 0.0
        sl = float(signal.get("stop_loss", 0) or 0)
        sl_edge_bps = (abs(price - sl) / price * 10000.0 * 2.0) if sl > 0 else 0.0
        return max(tp_edge_bps, sl_edge_bps)

    def passes(self, signal, asset_class, live_spread_bps=None):
        """Return (passed, reason). Edge must clear ratio × cost."""
        if not self.enabled:
            return True, "cost model disabled"
        edge = self.expected_edge_bps(signal)
        cost = self.round_trip_cost_bps(asset_class, live_spread_bps)
        threshold = self.min_edge_cost_ratio * cost
        if edge < threshold:
            return False, (
                f"Cost gate: edge {edge:.0f}bps < {self.min_edge_cost_ratio:g}× "
                f"cost {cost:.0f}bps (need {threshold:.0f}bps)"
            )
        return True, "ok"
