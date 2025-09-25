"""Backtesting harness for the delta-neutral strategy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional

import numpy as np

from .market_data import FeatureFrame, simulate_feed
from .strategy import DeltaNeutralStrategy, StrategyConfig


@dataclass
class BacktestResult:
    timestamps: np.ndarray
    equity_curve: np.ndarray
    metrics: Dict[str, np.ndarray]

    @property
    def summary(self) -> Dict[str, float]:
        returns = np.diff(self.equity_curve) / np.maximum(self.equity_curve[:-1], 1e-8)
        sharpe = 0.0
        if returns.size > 0:
            volatility = returns.std()
            if volatility > 0:
                sharpe = (returns.mean() / volatility) * np.sqrt(24)
        running_max = np.maximum.accumulate(self.equity_curve)
        drawdowns = self.equity_curve / np.maximum(running_max, 1e-8) - 1.0
        avg_carry = float(self.metrics.get("carry", np.zeros_like(self.equity_curve)).mean())
        return {
            "final_equity": float(self.equity_curve[-1]) if self.equity_curve.size else 0.0,
            "annualized_sharpe": float(sharpe),
            "max_drawdown": float(drawdowns.min()) if drawdowns.size else 0.0,
            "avg_carry": avg_carry,
        }


def run_example_backtest(
    periods: int = 1_000,
    seed: Optional[int] = 7,
    config: Optional[StrategyConfig] = None,
) -> BacktestResult:
    if config is None:
        config = StrategyConfig()
    market = simulate_feed(periods=periods, seed=seed)
    strategy = DeltaNeutralStrategy(config)
    metrics_frame: FeatureFrame = strategy.run_backtest(market)
    equity_curve = metrics_frame.data["equity"]
    timestamps = metrics_frame.data["timestamp"]
    metrics = {key: value for key, value in metrics_frame.data.items() if key not in {"equity", "timestamp"}}
    return BacktestResult(timestamps=timestamps, equity_curve=equity_curve, metrics=metrics)
