"""Delta neutral perpetual strategy implementation."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, Optional

import numpy as np
import pandas as pd

from .market_data import compute_features
from .risk import PortfolioState, RiskLimits, RiskManager


@dataclass
class StrategyConfig:
    """Configuration for the delta-neutral strategy."""

    risk_limits: RiskLimits = field(default_factory=RiskLimits)
    funding_alpha: float = 0.4
    basis_alpha: float = 0.25
    vol_target: float = 0.15
    risk_aversion: float = 4.0
    hedge_rebalance: float = 0.05  # rebalance threshold for delta
    predictive_windows: Dict[str, int] = None

    def __post_init__(self) -> None:
        if self.predictive_windows is None:
            self.predictive_windows = {"funding": 24, "basis": 24, "vol": 48}


class DeltaNeutralStrategy:
    """Stateful delta neutral strategy with regime detection and risk overlays."""

    def __init__(self, config: StrategyConfig) -> None:
        self.config = config
        self.risk = RiskManager(config.risk_limits)
        self.state = PortfolioState()
        self._last_hedge_price: Optional[float] = None

    def prepare_features(self, data: pd.DataFrame) -> pd.DataFrame:
        features = compute_features([data])
        windows = self.config.predictive_windows
        features["funding_signal"] = (
            features["funding_rate"].rolling(window=windows["funding"]).mean()
        )
        features["basis_signal"] = (
            features["basis"].rolling(window=windows["basis"]).mean()
        )
        features["vol_signal"] = (
            features["realized_vol"].rolling(window=windows["vol"]).mean()
        )
        return features.dropna().copy()

    def _expected_carry(self, row: pd.Series) -> float:
        funding_view = self.config.funding_alpha * row["funding_signal"]
        basis_cost = self.config.basis_alpha * row["basis_signal"]
        return funding_view - basis_cost

    def _target_perp_position(self, row: pd.Series, equity: float) -> float:
        expected_carry = self._expected_carry(row)
        volatility = max(row["vol_signal"], 1e-4)
        risk_unit = expected_carry / (self.config.risk_aversion * volatility**2)
        notional = np.clip(risk_unit, -5.0, 5.0) * equity
        notional = self.risk.enforce_leverage(notional, equity, row["perp"])

        if not self.risk.check_var(row["realized_vol"], notional, equity):
            notional = 0.0

        return notional / row["perp"]

    def _target_spot_position(self, target_perp: float, row: pd.Series) -> float:
        net_delta = target_perp + self.state.spot_position
        if self._last_hedge_price is None:
            self._last_hedge_price = row["spot"]
        if abs(net_delta) < self.config.hedge_rebalance:
            return self.state.spot_position
        self._last_hedge_price = row["spot"]
        return -target_perp

    def step(self, row: pd.Series) -> Dict[str, float]:
        spot_price = row["spot"]
        perp_price = row["perp"]
        equity = self.state.equity(spot_price, perp_price)
        if equity <= 0:
            equity = 1_000.0  # bootstrap capital
            self.state.cash = equity

        target_perp = self._target_perp_position(row, equity)
        target_spot = self._target_spot_position(target_perp, row)

        self.state.perp_position = target_perp
        self.state.spot_position = target_spot

        carry = target_perp * perp_price * row["funding_rate"]
        self.state.cash += carry

        pnl = (
            self.state.perp_position * (row["perp_return"])
            + self.state.spot_position * (row["spot_return"])
        ) * spot_price
        self.state.cash += pnl

        return {
            "target_perp": target_perp,
            "target_spot": target_spot,
            "equity": self.state.equity(spot_price, perp_price),
            "carry": carry,
            "pnl": pnl,
            "net_delta": self.state.net_delta(spot_price),
            "realized_vol": row["realized_vol"],
        }

    def run_backtest(self, data: pd.DataFrame) -> pd.DataFrame:
        features = self.prepare_features(data)
        records = []
        for timestamp, row in features.iterrows():
            metrics = self.step(row)
            metrics["timestamp"] = timestamp
            records.append(metrics)
        return pd.DataFrame(records).set_index("timestamp")
