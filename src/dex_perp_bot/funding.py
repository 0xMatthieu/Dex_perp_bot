from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from .exchanges.aster import AsterClient
from .exchanges.hyperliquid import HyperliquidClient


@dataclass(frozen=True)
class FundingRate:
    """Standardized funding rate information."""
    symbol: str
    rate: Decimal
    apy: Decimal
    next_funding_time_ms: Optional[int]


@dataclass(frozen=True)
class FundingComparison:
    """Represents a potential delta-neutral funding rate strategy."""
    symbol: str
    long_venue: str
    short_venue: str
    apy_difference: Decimal
    funding_is_imminent: bool
    next_funding_time_ms: Optional[int]

    def __str__(self) -> str:
        imminent_str = " (IMMINENT)" if self.funding_is_imminent else ""
        return (
            f"Long {self.symbol} on {self.long_venue}, Short on {self.short_venue}: "
            f"APY Difference = {self.apy_difference:.4f}%{imminent_str}"
        )


def _get_next_aster_funding_time_ms() -> int:
    """
    Calculates the next funding time for Aster, assuming funding at 00, 08, 16 UTC.
    """
    now = datetime.now(timezone.utc)
    funding_hours = [0, 4, 8, 12, 16, 20]

    next_funding_dt = None
    for hour in funding_hours:
        potential_dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
        if potential_dt > now:
            next_funding_dt = potential_dt
            break

    if next_funding_dt is None:  # all of today's funding times have passed
        tomorrow = now + timedelta(days=1)
        next_funding_dt = tomorrow.replace(hour=funding_hours[0], minute=0, second=0, microsecond=0)

    return int(next_funding_dt.timestamp() * 1000)


def _calculate_apy(rate: Decimal, periods_per_day: int) -> Decimal:
    """Calculate annualized percentage rate from a funding rate."""
    return rate * periods_per_day * 365 * 100


def _parse_aster_funding_rates(raw_rates: List[Dict]) -> Dict[str, FundingRate]:
    """Parse and normalize funding rates from Aster."""
    parsed: Dict[str, FundingRate] = {}
    # Calculate the single next funding time for all Aster pairs.
    next_funding_time_ms = _get_next_aster_funding_time_ms()

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
        parsed[normalized_symbol] = FundingRate(
            symbol=normalized_symbol, rate=rate, apy=apy, next_funding_time_ms=next_funding_time_ms
        )
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
        funding_time_ms_raw = hl_rate_info.get("nextFundingTime")
        funding_time_ms = int(funding_time_ms_raw) if funding_time_ms_raw is not None else None
        if not rate_str:
            continue

        rate = Decimal(rate_str)
        # Hyperliquid funding is hourly (24 times a day)
        apy = _calculate_apy(rate, periods_per_day=24)
        parsed[symbol] = FundingRate(
            symbol=symbol, rate=rate, apy=apy, next_funding_time_ms=funding_time_ms
        )
    return parsed


def fetch_and_compare_funding_rates(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    imminent_funding_minutes: int = 5,
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

    current_time_ms = int(time.time() * 1000)
    minutes_to_ms = imminent_funding_minutes * 60 * 1000

    comparisons: List[FundingComparison] = []
    for symbol in common_symbols:
        aster_rate = aster_rates[symbol]
        hyperliquid_rate = hyperliquid_rates[symbol]

        # Scenario 1: Long Aster, Short Hyperliquid
        funding_is_imminent_s1 = False
        next_funding_time_s1 = aster_rate.next_funding_time_ms
        if next_funding_time_s1:
            time_diff_ms = next_funding_time_s1 - current_time_ms
            if 0 < time_diff_ms <= minutes_to_ms:
                funding_is_imminent_s1 = True

        comparisons.append(FundingComparison(
            symbol=symbol,
            long_venue="Aster",
            short_venue="Hyperliquid",
            apy_difference=aster_rate.apy - hyperliquid_rate.apy,
            funding_is_imminent=funding_is_imminent_s1,
            next_funding_time_ms=next_funding_time_s1,
        ))

        # Scenario 2: Long Hyperliquid, Short Aster
        funding_is_imminent_s2 = False
        next_funding_time_s2 = hyperliquid_rate.next_funding_time_ms
        if next_funding_time_s2:
            time_diff_ms = next_funding_time_s2 - current_time_ms
            if 0 < time_diff_ms <= minutes_to_ms:
                funding_is_imminent_s2 = True

        comparisons.append(FundingComparison(
            symbol=symbol,
            long_venue="Hyperliquid",
            short_venue="Aster",
            apy_difference=hyperliquid_rate.apy - aster_rate.apy,
            funding_is_imminent=funding_is_imminent_s2,
            next_funding_time_ms=next_funding_time_s2,
        ))

    # Sort by the highest APY difference
    sorted_comparisons = sorted(comparisons, key=lambda x: x.apy_difference, reverse=True)

    top_4_comparisons = sorted_comparisons[:4]

    print("\n--- Top 4 Funding Rate Arbitrage Opportunities ---")
    if not top_4_comparisons:
        print("No opportunities found.")
    else:
        for comp in top_4_comparisons:
            print(comp)

    return top_4_comparisons
