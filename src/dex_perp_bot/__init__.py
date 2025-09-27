"""Dex Perp Bot package."""

from .config import Settings, HyperliquidCredentials, AsterCredentials, AsterConfig
from .funding import fetch_and_compare_funding_rates
from .strategy import determine_strategy, execute_strategy

__all__ = [
    "Settings",
    "HyperliquidCredentials",
    "AsterCredentials",
    "AsterConfig",
    "fetch_and_compare_funding_rates",
    "determine_strategy",
    "execute_strategy",
]
