"""Risk management primitives for the delta-neutral strategy."""
from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass
class RiskLimits:
    """Container for hard risk limits."""

    max_leverage: float = 3.0
    max_inventory: float = 10.0  # measured in BTC notional
    max_var: float = 0.03  # 1 day 95% VaR as fraction of equity


@dataclass
class PortfolioState:
    """State of the book for spot and perpetual legs."""

    spot_position: float = 0.0
    perp_position: float = 0.0
    cash: float = 0.0

    def net_delta(self, spot_price: float) -> float:
        """Return net delta of the book."""
        return self.spot_position + self.perp_position

    def equity(self, spot_price: float, perp_price: float) -> float:
        """Compute portfolio equity using mark-to-market."""
        spot_value = self.spot_position * spot_price
        perp_value = self.perp_position * perp_price
        return self.cash + spot_value + perp_value


class RiskManager:
    """Monitor risk metrics and determine target position sizing."""

    def __init__(self, limits: RiskLimits) -> None:
        self.limits = limits

    def enforce_leverage(self, target_notional: float, equity: float, price: float) -> float:
        """Clamp target notional to the leverage constraint."""
        if equity <= 0:
            return 0.0
        max_notional = self.limits.max_leverage * equity / price
        return float(np.clip(target_notional, -max_notional, max_notional))

    def enforce_inventory(self, target: float) -> float:
        """Clamp inventory to the configured limit."""
        return float(np.clip(target, -self.limits.max_inventory, self.limits.max_inventory))

    def forecast_var(self, volatility: float, notional: float) -> float:
        """Estimate a 1-day 95% VaR assuming log-normal returns."""
        horizon = np.sqrt(24)  # convert hourly vol to daily
        return 1.65 * volatility * horizon * abs(notional)

    def check_var(self, volatility: float, notional: float, equity: float) -> bool:
        var = self.forecast_var(volatility, notional)
        return var <= self.limits.max_var * equity
