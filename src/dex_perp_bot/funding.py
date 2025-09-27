from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Dict, List

from .exchanges.aster import AsterClient
from .exchanges.hyperliquid import HyperliquidClient


@dataclass(frozen=True)
class FundingRate:
    """Standardized funding rate information."""
    symbol: str
    rate: Decimal
    apy: Decimal


@dataclass(frozen=True)
class FundingComparison:
    """Represents a potential delta-neutral funding rate strategy."""
    symbol: str
    long_venue: str
    short_venue: str
    apy_difference: Decimal

    def __str__(self) -> str:
        return (
            f"Long {self.symbol} on {self.long_venue}, Short on {self.short_venue}: "
            f"APY Difference = {self.apy_difference:.4f}%"
        )


def _calculate_apy(rate: Decimal, periods_per_day: int) -> Decimal:
    """Calculate annualized percentage rate from a funding rate."""
    return rate * periods_per_day * 365 * 100


def _parse_aster_funding_rates(raw_rates: List[Dict]) -> Dict[str, FundingRate]:
    """Parse and normalize funding rates from Aster."""
    parsed: Dict[str, FundingRate] = {}
    for item in raw_rates:
        symbol = item.get("symbol")
        rate_str = item.get("fundingRate")
        if not symbol or not rate_str:
            continue

        # Normalize symbol from BTCUSDT -> BTC
        normalized_symbol = symbol.replace("USDT", "").replace("USD", "")
        rate = Decimal(rate_str)
        # Aster funding is typically every 8 hours (3 times a day)
        apy = _calculate_apy(rate, periods_per_day=3)
        parsed[normalized_symbol] = FundingRate(symbol=normalized_symbol, rate=rate, apy=apy)
    return parsed


def _parse_hyperliquid_funding_rates(raw_rates: List) -> Dict[str, FundingRate]:
    """Parse and normalize funding rates from Hyperliquid."""
    parsed: Dict[str, FundingRate] = {}
    for asset_data in raw_rates:
        if not isinstance(asset_data, list) or len(asset_data) < 2:
            continue
        symbol = asset_data[0]
        venues = asset_data[1]
        if not isinstance(venues, list):
            continue

        hl_venue_data = next((v for v in venues if isinstance(v, list) and len(v) > 1 and v[0] == "HlPerp"), None)
        if not hl_venue_data:
            continue

        hl_rate_info = hl_venue_data[1]
        if not isinstance(hl_rate_info, dict):
            continue

        rate_str = hl_rate_info.get("fundingRate")
        if not rate_str:
            continue

        rate = Decimal(rate_str)
        # Hyperliquid funding is hourly (24 times a day)
        apy = _calculate_apy(rate, periods_per_day=24)
        parsed[symbol] = FundingRate(symbol=symbol, rate=rate, apy=apy)
    return parsed


def fetch_and_compare_funding_rates(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
) -> List[FundingComparison]:
    """
    Fetches funding rates from Aster and Hyperliquid, compares them,
    prints all combinations, and returns the top 4 opportunities.
    """
    print("--- Fetching Funding Rates ---")
    aster_rates_raw = aster_client.get_funding_rate()
    hyperliquid_rates_raw = hyperliquid_client.get_predicted_funding_rates()

    print("--- Parsing and Comparing Funding Rates ---")
    aster_rates = _parse_aster_funding_rates(aster_rates_raw)
    hyperliquid_rates = _parse_hyperliquid_funding_rates(hyperliquid_rates_raw)

    common_symbols = sorted(list(set(aster_rates.keys()) & set(hyperliquid_rates.keys())))

    comparisons: List[FundingComparison] = []
    for symbol in common_symbols:
        aster_rate = aster_rates[symbol]
        hyperliquid_rate = hyperliquid_rates[symbol]

        # Scenario 1: Long Aster, Short Hyperliquid
        comparisons.append(FundingComparison(
            symbol=symbol,
            long_venue="Aster",
            short_venue="Hyperliquid",
            apy_difference=aster_rate.apy - hyperliquid_rate.apy,
        ))

        # Scenario 2: Long Hyperliquid, Short Aster
        comparisons.append(FundingComparison(
            symbol=symbol,
            long_venue="Hyperliquid",
            short_venue="Aster",
            apy_difference=hyperliquid_rate.apy - aster_rate.apy,
        ))

    # Sort by the highest APY difference
    sorted_comparisons = sorted(comparisons, key=lambda x: x.apy_difference, reverse=True)

    print("\n--- All Funding Rate Arbitrage Opportunities (Sorted) ---")
    for comp in sorted_comparisons:
        print(comp)

    return sorted_comparisons[:4]
