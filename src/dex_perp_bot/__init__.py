"""Dex Perp Bot package."""

from .config import Settings, HyperliquidCredentials, AsterCredentials, AsterConfig
from .funding import fetch_and_compare_funding_rates
from .strategy import perform_hourly_rebalance, execute_strategy, cleanup_all_open_positions_and_orders, report_portfolio_status

__all__ = [
    "Settings",
    "HyperliquidCredentials",
    "AsterCredentials",
    "AsterConfig",
    "fetch_and_compare_funding_rates",
    "perform_hourly_rebalance",
    "execute_strategy",
    "cleanup_all_open_positions_and_orders",
    "report_portfolio_status",
]
