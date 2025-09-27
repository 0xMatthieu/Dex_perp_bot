from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from .funding import FundingComparison, fetch_and_compare_funding_rates

if TYPE_CHECKING:
    from .exchanges.aster import AsterClient
    from .exchanges.hyperliquid import HyperliquidClient

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class StrategyDecision:
    """Represents a fully-formed delta-neutral trade."""
    opportunity: FundingComparison
    long_qty: Decimal
    short_qty: Decimal
    long_symbol: str
    short_symbol: str
    margin: Decimal
    leverage: int


def determine_strategy(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    leverage: int,
    capital_usd: Decimal,
    min_apy_diff_pct: Decimal = Decimal("0"),
) -> Optional[StrategyDecision]:
    """
    Analyzes funding rates and market data to determine the best delta-neutral strategy.
    """
    # 1. Find best funding opportunity
    opportunities = fetch_and_compare_funding_rates(aster_client, hyperliquid_client)
    if not opportunities:
        logger.info("No common symbols with funding rates found.")
        return None

    # Filter for imminent opportunities with a positive APY difference
    imminent_opportunities = [
        opp for opp in opportunities if opp.funding_is_imminent and opp.apy_difference > 0
    ]

    if not imminent_opportunities:
        logger.info("No imminent funding opportunities with positive APY difference found.")
        return None

    best_opp = imminent_opportunities[0]  # Already sorted by apy_difference
    if best_opp.apy_difference < min_apy_diff_pct:
        logger.info(
            f"Best opportunity APY diff {best_opp.apy_difference:.4f}% is below threshold {min_apy_diff_pct:.4f}%. No action."
        )
        return None

    logger.info(f"Identified best opportunity: {best_opp}")

    # 2. Prepare symbols and clients for the chosen opportunity
    symbol_base = best_opp.symbol
    symbol_hl = f"{symbol_base}/USDC:USDC"
    symbol_aster = f"{symbol_base}USDT"

    long_venue_client = aster_client if best_opp.long_venue == "Aster" else hyperliquid_client
    short_venue_client = hyperliquid_client if best_opp.long_venue == "Aster" else aster_client
    
    long_symbol = symbol_aster if isinstance(long_venue_client, AsterClient) else symbol_hl
    short_symbol = symbol_aster if isinstance(short_venue_client, AsterClient) else symbol_hl

    # 3. Get prices and filters for sizing
    logger.info("Fetching prices and exchange info for sizing...")
    price_long = long_venue_client.get_price(long_symbol)
    price_short = short_venue_client.get_price(short_symbol)

    # 4. Calculate quantities based on capital and leverage
    notional_value = capital_usd * Decimal(leverage)
    qty_long = notional_value / price_long
    qty_short = notional_value / price_short

    # 5. Round quantities to exchange-specific lot sizes
    if isinstance(long_venue_client, AsterClient):
        filters = long_venue_client.get_symbol_filters(long_symbol)
        step_size = filters['step_size']
        qty_long = (qty_long // step_size) * step_size
    else:  # Hyperliquid
        market = long_venue_client._client.market(long_symbol)
        step_size = Decimal(str(market['precision']['amount']))
        qty_long = (qty_long // step_size) * step_size

    if isinstance(short_venue_client, AsterClient):
        filters = short_venue_client.get_symbol_filters(short_symbol)
        step_size = filters['step_size']
        qty_short = (qty_short // step_size) * step_size
    else:  # Hyperliquid
        market = short_venue_client._client.market(short_symbol)
        step_size = Decimal(str(market['precision']['amount']))
        qty_short = (qty_short // step_size) * step_size
        
    if qty_long == 0 or qty_short == 0:
        logger.error("Calculated quantity is zero. Increase capital or leverage.")
        return None

    return StrategyDecision(
        opportunity=best_opp,
        long_qty=qty_long,
        short_qty=qty_short,
        long_symbol=long_symbol,
        short_symbol=short_symbol,
        margin=capital_usd,
        leverage=leverage,
    )


def execute_strategy(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    decision: StrategyDecision,
) -> None:
    """
    Executes a pre-determined strategy by setting leverage and placing orders.
    """
    logger.info(f"--- Executing Delta-Neutral Strategy for {decision.opportunity.symbol} ---")
    
    long_venue_client = aster_client if decision.opportunity.long_venue == "Aster" else hyperliquid_client
    short_venue_client = hyperliquid_client if decision.opportunity.long_venue == "Aster" else aster_client

    # 1. Set leverage on both exchanges
    logger.info("Setting leverage to %sx on both venues...", decision.leverage)
    long_venue_client.set_leverage(decision.long_symbol, decision.leverage)
    short_venue_client.set_leverage(decision.short_symbol, decision.leverage)

    # 2. Place opposing market orders
    logger.info("Placing orders to establish positions...")
    
    long_qty = decision.long_qty if isinstance(long_venue_client, AsterClient) else float(decision.long_qty)
    short_qty = decision.short_qty if isinstance(short_venue_client, AsterClient) else float(decision.short_qty)

    long_order_res = long_venue_client.place_order(
        symbol=decision.long_symbol, side="BUY", order_type="MARKET", quantity=long_qty
    )
    logger.info(f"Long order ({decision.opportunity.long_venue}) response: {long_order_res}")

    short_order_res = short_venue_client.place_order(
        symbol=decision.short_symbol, side="SELL", order_type="MARKET", quantity=short_qty
    )
    logger.info(f"Short order ({decision.opportunity.short_venue}) response: {short_order_res}")
    
    logger.info("Strategy execution complete.")
