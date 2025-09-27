"""Dex Perp Bot package."""

from .config import Settings, HyperliquidCredentials, AsterCredentials, AsterConfig
from .funding import fetch_and_compare_funding_rates

__all__ = [
    "Settings",
    "HyperliquidCredentials",
    "AsterCredentials",
    "AsterConfig",
    "fetch_and_compare_funding_rates",
]
