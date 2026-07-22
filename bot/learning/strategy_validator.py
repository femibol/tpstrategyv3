"""Strategy validation & robustness report — "real edge or lucky streak?"

Built after the 2026-07-22 finding that crypto mean_reversion's headline
+$393 was ~95% driven by its top 5 trades (two symbols, one 3-day window) and
turned NEGATIVE once the top ~10 were removed. Net P&L alone hid that. This
module scores each strategy against a pre-committed go-live bar so a lucky
streak can never again masquerade as an edge.

The go-live bar — ALL must pass for a strategy to GRADUATE:
  1. n >= min_trades          enough sample to mean anything
  2. expectancy > 0           makes money per trade (net of realised costs)
  3. profit_factor >= pf_min  wins outweigh losses with margin
  4. net_ex_top > 0           SURVIVES removing the luckiest `top_pct` trades
  5. recent_third_net > 0     still working in the most recent third (OOS proxy)

Everything here is a pure function of a trade list (dicts with at least
`pnl`, `strategy`, `symbol`, `exit_time`), so it is trivially testable and can
be driven from the dashboard, a CLI, or a test.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

# Default go-live bar. Deliberately strict — the whole point is to not fool
# ourselves. Override per-call if a sleeve legitimately trades less often.
DEFAULT_BAR = {
    "min_trades": 150,
    "pf_min": 1.3,
    "top_pct": 0.05,   # fraction of luckiest trades to strip for the robustness test
}

_CRYPTO_SUFFIXES = ("-USD", "-USDT", "-BTC", "-ETH")


def is_crypto(symbol: str) -> bool:
    return any(str(symbol).upper().endswith(s) for s in _CRYPTO_SUFFIXES)


def _pnls(trades):
    return [float(t.get("pnl", 0) or 0) for t in trades]


def core_metrics(trades) -> dict:
    """Expectancy / win-rate / profit-factor and the win/loss shape."""
    p = _pnls(trades)
    n = len(p)
    if n == 0:
        return {"n": 0, "net": 0.0, "expectancy": 0.0, "win_rate": 0.0,
                "profit_factor": 0.0, "avg_win": 0.0, "avg_loss": 0.0}
    wins = [x for x in p if x > 0]
    losses = [x for x in p if x <= 0]
    gross_win = sum(wins)
    gross_loss = abs(sum(losses))
    return {
        "n": n,
        "net": sum(p),
        "expectancy": sum(p) / n,
        "win_rate": len(wins) / n * 100.0,
        # inf only when there are wins and zero loss; 0.0 when no wins at all
        "profit_factor": (gross_win / gross_loss) if gross_loss > 0
                         else (float("inf") if gross_win > 0 else 0.0),
        "avg_win": (gross_win / len(wins)) if wins else 0.0,
        "avg_loss": (sum(losses) / len(losses)) if losses else 0.0,
    }


def robustness_metrics(trades, top_pct: float = 0.05) -> dict:
    """Concentration + recency checks — the tests net P&L hides.

    - net_ex_top:    net after removing the top `top_pct` trades by pnl. A real
                     edge stays positive; a concentrated one collapses.
    - top_share:     fraction of total net contributed by those top trades
                     (only meaningful when net > 0).
    - recent_third_net: net over the most recent third of trades by exit_time —
                     a cheap out-of-sample / decay check.
    """
    p = sorted(_pnls(trades), reverse=True)
    n = len(p)
    net = sum(p)
    k = max(1, int(n * top_pct)) if n else 0
    net_ex_top = sum(p[k:]) if n else 0.0
    top_net = sum(p[:k]) if n else 0.0

    # Recency: order by exit_time (fall back to input order if unparseable).
    def _key(t):
        try:
            return datetime.fromisoformat(t["exit_time"])
        except Exception:
            return datetime.min
    ordered = sorted(trades, key=_key)
    third = max(1, n // 3) if n else 0
    recent_third_net = sum(_pnls(ordered[-third:])) if n else 0.0

    return {
        "removed": k,
        "net_ex_top": net_ex_top,
        "top_net": top_net,
        "top_share": (top_net / net) if net > 0 else None,
        "recent_third_n": third,
        "recent_third_net": recent_third_net,
    }


@dataclass
class Verdict:
    strategy: str
    graduated: bool
    metrics: dict
    robustness: dict
    failures: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "strategy": self.strategy,
            "graduated": self.graduated,
            "verdict": "GRADUATED" if self.graduated else "NOT READY",
            "failures": self.failures,
            **self.metrics,
            **self.robustness,
        }


def evaluate(strategy: str, trades, bar: dict | None = None) -> Verdict:
    """Score one strategy's trades against the go-live bar."""
    b = {**DEFAULT_BAR, **(bar or {})}
    m = core_metrics(trades)
    r = robustness_metrics(trades, top_pct=b["top_pct"])

    failures = []
    if m["n"] < b["min_trades"]:
        failures.append(f"sample too small ({m['n']}<{b['min_trades']})")
    if m["expectancy"] <= 0:
        failures.append(f"expectancy not positive (${m['expectancy']:.2f}/trade)")
    if m["profit_factor"] < b["pf_min"]:
        pf = "inf" if m["profit_factor"] == float("inf") else f"{m['profit_factor']:.2f}"
        failures.append(f"profit factor below {b['pf_min']} ({pf})")
    if r["net_ex_top"] <= 0:
        failures.append(
            f"collapses without top {r['removed']} trades "
            f"(${r['net_ex_top']:.2f})"
        )
    if r["recent_third_net"] <= 0:
        failures.append(f"recent third negative (${r['recent_third_net']:.2f})")

    return Verdict(strategy, not failures, m, r, failures)


def build_report(trade_history, bar: dict | None = None) -> dict:
    """Full report: per-strategy, plus overall and per-asset-class rollups."""
    by_strategy = {}
    for t in trade_history:
        by_strategy.setdefault(t.get("strategy", "unknown"), []).append(t)

    strategies = [
        evaluate(name, rows, bar).to_dict()
        for name, rows in sorted(
            by_strategy.items(), key=lambda kv: -sum(_pnls(kv[1]))
        )
    ]
    crypto = [t for t in trade_history if is_crypto(t.get("symbol", ""))]
    equity = [t for t in trade_history if not is_crypto(t.get("symbol", ""))]
    return {
        "bar": {**DEFAULT_BAR, **(bar or {})},
        "overall": {**core_metrics(trade_history),
                    **robustness_metrics(trade_history, (bar or DEFAULT_BAR)["top_pct"]
                                         if bar else DEFAULT_BAR["top_pct"])},
        "crypto": {**core_metrics(crypto)},
        "equity": {**core_metrics(equity)},
        "strategies": strategies,
    }


def format_report(report: dict) -> str:
    """Human-readable table for a CLI / log."""
    b = report["bar"]
    lines = []
    lines.append(
        f"GO-LIVE BAR: n>={b['min_trades']}, expectancy>0, PF>={b['pf_min']}, "
        f"positive after removing top {b['top_pct']:.0%}, recent-third>0"
    )
    o = report["overall"]
    pf = "inf" if o["profit_factor"] == float("inf") else f"{o['profit_factor']:.2f}"
    lines.append(
        f"OVERALL: {o['n']}t  net ${o['net']:.2f}  exp ${o['expectancy']:.2f}  "
        f"PF {pf}  ex-top ${o['net_ex_top']:.2f}  recent3 ${o['recent_third_net']:.2f}"
    )
    c, e = report["crypto"], report["equity"]
    lines.append(f"  crypto {c['n']}t ${c['net']:.2f} (exp ${c['expectancy']:.2f})   "
                 f"equity {e['n']}t ${e['net']:.2f} (exp ${e['expectancy']:.2f})")
    lines.append("")
    header = (f"{'strategy':<20}{'n':>5}{'net$':>10}{'exp$':>8}{'PF':>6}"
              f"{'ex-top$':>10}{'recent3$':>10}  verdict")
    lines.append(header)
    for s in report["strategies"]:
        pf = "inf" if s["profit_factor"] == float("inf") else f"{s['profit_factor']:.2f}"
        mark = "✅" if s["graduated"] else "❌"
        lines.append(
            f"{s['strategy']:<20}{s['n']:>5}{s['net']:>10.2f}{s['expectancy']:>8.2f}"
            f"{pf:>6}{s['net_ex_top']:>10.2f}{s['recent_third_net']:>10.2f}  {mark} {s['verdict']}"
        )
        if s["failures"]:
            lines.append(f"{'':<20}   ↳ {'; '.join(s['failures'])}")
    return "\n".join(lines)
