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
    apy_1h: Decimal
    apy_4h: Decimal
    next_funding_time_ms: Optional[int]


@dataclass(frozen=True)
class FundingComparison:
    """Represents a potential delta-neutral funding rate strategy."""
    symbol: str
    long_venue: str
    short_venue: str
    apy_difference: Decimal
    apy_difference_basis: str  # "1h" or "4h"
    apy_aster_1h: Decimal
    apy_aster_4h: Decimal
    apy_hyperliquid_1h: Decimal
    apy_hyperliquid_4h: Decimal
    rate_aster: Decimal
    rate_hyperliquid: Decimal
    funding_is_imminent: bool
    next_funding_time_ms: Optional[int]
    long_max_leverage: Optional[int]
    short_max_leverage: Optional[int]
    is_actionable: bool

    def __str__(self) -> str:
        imminent_str = " (IMMINENT)" if self.funding_is_imminent else ""
        actionable_str = "" if self.is_actionable else " (NOT ACTIONABLE)"
        leverage_str = f"Lvg: {self.long_max_leverage or 'N/A'}x/{self.short_max_leverage or 'N/A'}x"
        details_str = (
            f"Aster APY (1h/4h): {self.apy_aster_1h:.4f}%/{self.apy_aster_4h:.4f}% (Rate: {self.rate_aster:.8f}) | "
            f"HL APY (1h/4h): {self.apy_hyperliquid_1h:.4f}%/{self.apy_hyperliquid_4h:.4f}% (Rate: {self.rate_hyperliquid:.8f})"
        )
        return (
            f"Long {self.symbol} on {self.long_venue}, Short on {self.short_venue}: "
            f"APY Difference ({self.apy_difference_basis} basis) = {self.apy_difference:.4f}%{imminent_str}{actionable_str} | {leverage_str} | {details_str}"
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
        apy_4h = _calculate_apy(rate, periods_per_day=6)
        apy_1h = apy_4h / 4

        parsed[normalized_symbol] = FundingRate(
            symbol=normalized_symbol,
            rate=rate,
            apy_1h=apy_1h,
            apy_4h=apy_4h,
            next_funding_time_ms=next_funding_time_ms,
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

        rate_str = hl_rate_info.get("fundingRate")
        if not rate_str:
            continue

        rate = Decimal(rate_str)
        # Hyperliquid funding is hourly. We calculate the next one manually to be safe.
        now = datetime.now(timezone.utc)
        next_hour = (now + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
        next_funding_time_ms = int(next_hour.timestamp() * 1000)

        apy_1h = _calculate_apy(rate, periods_per_day=24)
        apy_4h = apy_1h * 4
        parsed[symbol] = FundingRate(
            symbol=symbol,
            rate=rate,
            apy_1h=apy_1h,
            apy_4h=apy_4h,
            next_funding_time_ms=next_funding_time_ms,
        )
    return parsed


def fetch_and_compare_funding_rates(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    imminent_funding_minutes: int,
) -> List[FundingComparison]:
    """
    Fetches funding rates from Aster and Hyperliquid, compares them,
    prints all combinations, and returns the top 4 opportunities.
    """
    logger.info("--- Fetching Funding Rates ---")
    aster_rates_raw = aster_client.get_funding_rate()
    hyperliquid_rates_raw = hyperliquid_client.get_predicted_funding_rates()

    aster_rates = _parse_aster_funding_rates(aster_rates_raw, aster_client)
    hyperliquid_rates = _parse_hyperliquid_funding_rates(hyperliquid_rates_raw, hyperliquid_client)

    common_symbols = sorted(list(set(aster_rates.keys()) & set(hyperliquid_rates.keys())))

    current_time_ms = int(time.time() * 1000)
    minutes_to_ms = imminent_funding_minutes * 60 * 1000

    # Print current time and next funding times for context
    current_time_dt = datetime.fromtimestamp(current_time_ms / 1000, tz=timezone.utc)
    logger.info(f"Current Time:              {current_time_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if aster_rates:
        aster_next_funding_ms = next(iter(aster_rates.values())).next_funding_time_ms
        if aster_next_funding_ms:
            aster_next_funding_dt = datetime.fromtimestamp(aster_next_funding_ms / 1000, tz=timezone.utc)
            logger.info(f"Next Aster Funding Time:     {aster_next_funding_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")
    if hyperliquid_rates:
        hl_next_funding_ms = next(iter(hyperliquid_rates.values())).next_funding_time_ms
        if hl_next_funding_ms:
            hl_next_funding_dt = datetime.fromtimestamp(hl_next_funding_ms / 1000, tz=timezone.utc)
            logger.info(f"Next Hyperliquid Funding Time: {hl_next_funding_dt.strftime('%Y-%m-%d %H:%M:%S UTC')}")

    comparisons: List[FundingComparison] = []
    for symbol in common_symbols:
        aster_rate = aster_rates[symbol]
        hyperliquid_rate = hyperliquid_rates[symbol]

        # Check for imminent funding on either exchange.
        aster_funding_imminent = False
        if aster_rate.next_funding_time_ms:
            time_diff_ms = aster_rate.next_funding_time_ms - current_time_ms
            if 0 < time_diff_ms <= minutes_to_ms:
                aster_funding_imminent = True

        hl_funding_imminent = False
        if hyperliquid_rate.next_funding_time_ms:
            time_diff_ms = hyperliquid_rate.next_funding_time_ms - current_time_ms
            if 0 < time_diff_ms <= minutes_to_ms:
                hl_funding_imminent = True

        is_imminent = aster_funding_imminent or hl_funding_imminent

        # If Aster's funding is imminent, its 4-hour rate is the primary driver for an
        # opportunity. We should compare on that basis. Otherwise, use 1-hour.
        if aster_funding_imminent:
            apy_aster_basis = aster_rate.apy_4h
            apy_hl_basis = hyperliquid_rate.apy_4h
            apy_basis = "4h"
        else:
            apy_aster_basis = aster_rate.apy_1h
            apy_hl_basis = hyperliquid_rate.apy_1h
            apy_basis = "1h"

        # Scenario 1: Long Aster, Short Hyperliquid
        comparisons.append(FundingComparison(
            symbol=symbol,
            long_venue="Aster",
            short_venue="Hyperliquid",
            apy_difference=apy_aster_basis - apy_hl_basis,
            apy_difference_basis=apy_basis,
            apy_aster_1h=aster_rate.apy_1h,
            apy_aster_4h=aster_rate.apy_4h,
            apy_hyperliquid_1h=hyperliquid_rate.apy_1h,
            apy_hyperliquid_4h=hyperliquid_rate.apy_4h,
            rate_aster=aster_rate.rate,
            rate_hyperliquid=hyperliquid_rate.rate,
            funding_is_imminent=is_imminent,
            next_funding_time_ms=aster_rate.next_funding_time_ms,
            long_max_leverage=None,
            short_max_leverage=None,
            is_actionable=False,
        ))

        # Scenario 2: Long Hyperliquid, Short Aster
        comparisons.append(FundingComparison(
            symbol=symbol,
            long_venue="Hyperliquid",
            short_venue="Aster",
            apy_difference=apy_hl_basis - apy_aster_basis,
            apy_difference_basis=apy_basis,
            apy_aster_1h=aster_rate.apy_1h,
            apy_aster_4h=aster_rate.apy_4h,
            apy_hyperliquid_1h=hyperliquid_rate.apy_1h,
            apy_hyperliquid_4h=hyperliquid_rate.apy_4h,
            rate_aster=aster_rate.rate,
            rate_hyperliquid=hyperliquid_rate.rate,
            funding_is_imminent=is_imminent,
            next_funding_time_ms=hyperliquid_rate.next_funding_time_ms,
            long_max_leverage=None,
            short_max_leverage=None,
            is_actionable=False,
        ))

    # Sort by the highest APY difference
    sorted_comparisons = sorted(comparisons, key=lambda x: x.apy_difference, reverse=True)

    top_opportunities = sorted_comparisons[:4]

    # Enrich the top opportunities with market data
    enriched_opportunities = []
    for comp in top_opportunities:
        symbol_base = comp.symbol
        symbol_hl = f"{symbol_base}/USDC:USDC"
        symbol_aster = f"{symbol_base}USDT"

        try:
            if comp.long_venue == "Aster":
                long_leverage = aster_client.get_max_leverage(symbol_aster)
                short_leverage = hyperliquid_client.get_max_leverage(symbol_hl)
            else:  # long venue is Hyperliquid
                long_leverage = hyperliquid_client.get_max_leverage(symbol_hl)
                short_leverage = aster_client.get_max_leverage(symbol_aster)
            is_actionable = True
        except Exception as e:
            logger.warning(f"Could not get market data for {symbol_base}, marking as not actionable: {e}")
            long_leverage = None
            short_leverage = None
            is_actionable = False

        enriched_opportunities.append(FundingComparison(
            symbol=comp.symbol,
            long_venue=comp.long_venue,
            short_venue=comp.short_venue,
            apy_difference=comp.apy_difference,
            apy_difference_basis=comp.apy_difference_basis,
            apy_aster_1h=comp.apy_aster_1h,
            apy_aster_4h=comp.apy_aster_4h,
            apy_hyperliquid_1h=comp.apy_hyperliquid_1h,
            apy_hyperliquid_4h=comp.apy_hyperliquid_4h,
            rate_aster=comp.rate_aster,
            rate_hyperliquid=comp.rate_hyperliquid,
            funding_is_imminent=comp.funding_is_imminent,
            next_funding_time_ms=comp.next_funding_time_ms,
            long_max_leverage=long_leverage,
            short_max_leverage=short_leverage,
            is_actionable=is_actionable,
        ))

    logger.info("\n--- Top 4 Funding Rate Arbitrage Opportunities ---")
    if not enriched_opportunities:
        logger.info("No opportunities found.")
    else:
        for comp in enriched_opportunities:
            logger.info(comp)

    return enriched_opportunities
