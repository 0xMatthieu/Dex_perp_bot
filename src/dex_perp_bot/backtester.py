"""Backtesting harness for the delta-neutral strategy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pandas as pd

from .market_data import simulate_feed
from .strategy import DeltaNeutralStrategy, StrategyConfig


@dataclass
class BacktestResult:
    equity_curve: pd.Series
    metrics: pd.DataFrame

    @property
    def summary(self) -> pd.Series:
        returns = self.equity_curve.pct_change().dropna()
        sharpe = (returns.mean() / returns.std()) * (24**0.5) if not returns.empty else 0.0
        max_drawdown = (self.equity_curve / self.equity_curve.cummax() - 1).min()
        return pd.Series(
            {
                "final_equity": float(self.equity_curve.iloc[-1]),
                "annualized_sharpe": float(sharpe),
                "max_drawdown": float(max_drawdown),
                "avg_carry": float(self.metrics["carry"].mean()),
            }
        )


def run_example_backtest(
    periods: int = 1_000,
    seed: Optional[int] = 7,
    config: Optional[StrategyConfig] = None,
) -> BacktestResult:
    if config is None:
        config = StrategyConfig()
    market = simulate_feed(periods=periods, seed=seed)
    strategy = DeltaNeutralStrategy(config)
    metrics = strategy.run_backtest(market)
    equity_curve = metrics["equity"].ffill()
    return BacktestResult(equity_curve=equity_curve, metrics=metrics)
