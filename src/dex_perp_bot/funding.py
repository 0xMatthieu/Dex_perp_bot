from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Dict, List, Optional

from .exchanges.aster import AsterClient
from .exchanges.hyperliquid import HyperliquidClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FundingRate:
    """Standardized funding rate information."""
    symbol: str
    rate: Decimal
    apy: Decimal
    next_funding_time_ms: Optional[int]
    max_leverage: Optional[int] = None
    is_tradable: bool = False


@dataclass(frozen=True)
class FundingComparison:
    """Represents a potential delta-neutral funding rate strategy."""
    symbol: str
    long_venue: str
    short_venue: str
    apy_difference: Decimal
    funding_is_imminent: bool
    next_funding_time_ms: Optional[int]
    long_max_leverage: Optional[int]
    short_max_leverage: Optional[int]
    is_actionable: bool

    def __str__(self) -> str:
        imminent_str = " (IMMINENT)" if self.funding_is_imminent else ""
        actionable_str = "" if self.is_actionable else " (NOT ACTIONABLE)"
        leverage_str = f"Lvg: {self.long_max_leverage or 'N/A'}x/{self.short_max_leverage or 'N/A'}x"
        return (
            f"Long {self.symbol} on {self.long_venue}, Short on {self.short_venue}: "
            f"APY Difference = {self.apy_difference:.4f}%{imminent_str}{actionable_str} | {leverage_str}"
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


def _parse_aster_funding_rates(raw_rates: List[Dict], aster_client: AsterClient) -> Dict[str, FundingRate]:
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
        # Aster funding is every 4 hours (6 times a day)
        apy = _calculate_apy(rate, periods_per_day=6)

        max_leverage = None
        is_tradable = False
        try:
            max_leverage = aster_client.get_max_leverage(symbol)
            is_tradable = True
        except Exception as e:
            logger.debug(f"Could not get market data for {symbol} on Aster: {e}")

        parsed[normalized_symbol] = FundingRate(
            symbol=normalized_symbol,
            rate=rate,
            apy=apy,
            next_funding_time_ms=next_funding_time_ms,
            max_leverage=max_leverage,
            is_tradable=is_tradable,
        )
    return parsed


def _parse_hyperliquid_funding_rates(raw_rates: List, hyperliquid_client: HyperliquidClient) -> Dict[str, FundingRate]:
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

        hl_symbol = f"{symbol}/USDC:USDC"
        max_leverage = None
        is_tradable = False
        try:
            max_leverage = hyperliquid_client.get_max_leverage(hl_symbol)
            is_tradable = True
        except Exception as e:
            logger.debug(f"Could not get market data for {hl_symbol} on Hyperliquid: {e}")

        rate_str = hl_rate_info.get("fundingRate")
        if not rate_str:
            continue

        rate = Decimal(rate_str)
        # Hyperliquid funding is hourly. We calculate the next one manually to be safe.
        now = datetime.now(timezone.utc)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        next_funding_time_ms = int(next_hour.timestamp() * 1000)

        apy = _calculate_apy(rate, periods_per_day=24)
        parsed[symbol] = FundingRate(
            symbol=symbol,
            rate=rate,
            apy=apy,
            next_funding_time_ms=next_funding_time_ms,
            max_leverage=max_leverage,
            is_tradable=is_tradable,
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

        is_actionable = aster_rate.is_tradable and hyperliquid_rate.is_tradable

        comparisons.append(FundingComparison(
            symbol=symbol,
            long_venue="Aster",
            short_venue="Hyperliquid",
            apy_difference=aster_rate.apy - hyperliquid_rate.apy,
            funding_is_imminent=funding_is_imminent_s1,
            next_funding_time_ms=next_funding_time_s1,
            long_max_leverage=aster_rate.max_leverage,
            short_max_leverage=hyperliquid_rate.max_leverage,
            is_actionable=is_actionable,
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
            long_max_leverage=hyperliquid_rate.max_leverage,
            short_max_leverage=aster_rate.max_leverage,
            is_actionable=is_actionable,
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
