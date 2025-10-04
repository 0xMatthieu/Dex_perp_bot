from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING, Dict, List, Optional

from .funding import FundingComparison, fetch_and_compare_funding_rates
from .exchanges.aster import AsterClient
from .exchanges.hyperliquid import HyperliquidClient

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)


def report_portfolio_status(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
) -> None:
    """Fetches and logs PNL, size, and price spread for current positions."""
    logger.info("--- Current Portfolio Status ---")
    try:
        hl_positions = hyperliquid_client.get_all_positions()
        aster_positions = aster_client.get_all_positions()

        if not hl_positions and not aster_positions:
            logger.info("No open positions on either exchange.")
            return

        total_pnl = Decimal("0")

        # Process Hyperliquid positions
        for pos in hl_positions:
            symbol_base = pos.get("symbol", "").split('/')[0]
            pnl = Decimal(pos.get("unrealizedPnl", "0"))
            total_pnl += pnl
            logger.info(
                f"Hyperliquid Position: {pos.get('symbol')} | Size: {pos.get('contracts')} | "
                f"Side: {pos.get('side')} | PNL: ${pnl:.4f}"
            )

        # Process Aster positions
        for pos in aster_positions:
            symbol_base = pos.get("symbol", "").replace("USDT", "")
            try:
                pnl = Decimal(pos.get("unrealizedProfit", "0"))
                total_pnl += pnl
                logger.info(
                    f"Aster Position: {pos.get('symbol')} | Size: {pos.get('positionAmt')} | PNL: ${pnl:.4f}"
                )
            except InvalidOperation:
                logger.warning(f"Could not parse PNL for Aster position: {pos}")

        logger.info(f"Total Unrealized PNL: ${total_pnl:.4f}")

        # Calculate and log price spread if in a delta-neutral position
        if len(hl_positions) == 1 and len(aster_positions) == 1:
            hl_symbol = hl_positions[0].get('symbol')
            aster_symbol = aster_positions[0].get('symbol')
            if hl_symbol and aster_symbol:
                price_hl = hyperliquid_client.get_price(hl_symbol)
                price_aster = aster_client.get_price(aster_symbol)
                spread = price_aster - price_hl
                spread_pct = (spread / price_hl) * 100 if price_hl else Decimal("0")
                logger.info(
                    f"Price Spread ({aster_symbol}): Aster=${price_aster:.4f}, HL=${price_hl:.4f} | "
                    f"Delta: ${spread:.4f} ({spread_pct:.4f}%)"
                )

    except Exception as exc:
        logger.error(f"Failed to generate portfolio status report: {exc}")


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


def _is_portfolio_matching_opportunity(
    hl_positions: List[Dict],
    aster_positions: List[Dict],
    opportunity: FundingComparison,
) -> bool:
    """Checks if the current open positions match the target opportunity."""
    if not opportunity:
        return not hl_positions and not aster_positions

    target_symbol_base = opportunity.symbol
    target_long_venue = opportunity.long_venue

    # For simplicity, assume only one position pair should be open for this strategy.
    if len(hl_positions) > 1 or len(aster_positions) > 1 or (len(hl_positions) != len(aster_positions)):
        return False  # Not in a clean delta-neutral state

    if not hl_positions:  # and not aster_positions
        return False  # No positions exist

    hl_pos = hl_positions[0]
    aster_pos = aster_positions[0]

    hl_symbol_base = hl_pos.get("symbol", "").split('/')[0]
    aster_symbol_base = aster_pos.get("symbol", "").replace("USDT", "")

    if not (hl_symbol_base == aster_symbol_base == target_symbol_base):
        return False  # Wrong symbol

    # Check sides
    hl_side = hl_pos.get("side")  # 'long' or 'short'
    aster_pos_amt = Decimal(aster_pos.get("positionAmt", "0"))
    aster_side = 'long' if aster_pos_amt > 0 else 'short'

    if target_long_venue == "Hyperliquid":
        return hl_side == 'long' and aster_side == 'short'
    else:  # Long on Aster
        return aster_side == 'long' and hl_side == 'short'


def _calculate_trade_decision(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    best_opp: FundingComparison,
    leverage: int,
    capital_usd: Decimal,
) -> Optional[StrategyDecision]:
    """Calculates the quantities and details for a given strategy opportunity."""
    # 1. Prepare symbols and clients for the chosen opportunity
    symbol_base = best_opp.symbol
    symbol_hl = f"{symbol_base}/USDC:USDC"
    symbol_aster = f"{symbol_base}USDT"

    long_venue_client = aster_client if best_opp.long_venue == "Aster" else hyperliquid_client
    short_venue_client = hyperliquid_client if best_opp.long_venue == "Aster" else aster_client

    long_symbol = symbol_aster if isinstance(long_venue_client, AsterClient) else symbol_hl
    short_symbol = symbol_aster if isinstance(short_venue_client, AsterClient) else symbol_hl

    # 2. Get prices and filters for sizing
    logger.info("Fetching prices and exchange info for sizing...")
    price_long = long_venue_client.get_price(long_symbol)
    price_short = short_venue_client.get_price(short_symbol)

    # 3. Calculate quantities based on capital and leverage
    notional_value = capital_usd * Decimal(leverage)
    qty_long = notional_value / price_long
    qty_short = notional_value / price_short

    # 4. Round quantities to exchange-specific lot sizes
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


def perform_hourly_rebalance(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    leverage: int,
    capital_usd: Decimal,
    min_apy_diff_pct: Decimal,
    min_spread_pct: Decimal,
) -> None:
    """
    Main strategy function to rebalance the portfolio hourly to the best opportunity.
    It closes existing positions and opens a new one based on funding and spread.
    """
    logger.info("--- Performing Hourly Rebalance ---")

    # 1. Find all opportunities.
    opportunities = fetch_and_compare_funding_rates(
        aster_client, hyperliquid_client, imminent_funding_minutes=60 # Use a wide window
    )
    actionable_opportunities = [
        opp for opp in opportunities if opp.is_actionable and abs(opp.apy_difference) > min_apy_diff_pct
    ]

    if not actionable_opportunities:
        logger.info("No actionable opportunities found meeting the minimum APY difference criteria. Waiting for next cycle.")
        return

    best_opp = actionable_opportunities[0]

    # 2. Determine effective leverage.
    effective_leverage = min(
        leverage, best_opp.long_max_leverage or 1, best_opp.short_max_leverage or 1
    )
    logger.info(f"Selected best opportunity: {best_opp}")
    logger.info(f"Effective leverage set to {effective_leverage}x.")

    # 3. Check if the current portfolio already matches the best opportunity.
    hl_positions = hyperliquid_client.get_all_positions()
    aster_positions = aster_client.get_all_positions()
    logger.info(f"Current positions: Hyperliquid={hl_positions}, Aster={aster_positions}")

    if _is_portfolio_matching_opportunity(hl_positions, aster_positions, best_opp):
        logger.info("Already in optimal position for imminent funding. Holding position.")
        return

    # 4. Calculate the new trade.
    decision = _calculate_trade_decision(
        aster_client, hyperliquid_client, best_opp, effective_leverage, capital_usd
    )
    if not decision:
        logger.error("Failed to calculate trade decision. Aborting rebalance.")
        return

    # 5. Close all open positions and orders
    logger.info("Closing all existing positions and orders before finding new opportunity...")
    cleanup_all_open_positions_and_orders(aster_client, hyperliquid_client, timeout_seconds=900)
    time.sleep(15)  # Allow time for balance updates after closing positions.

    # 6. Execute the trade.
    execute_strategy(aster_client, hyperliquid_client, decision)


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
        symbol=decision.long_symbol, side="BUY", order_type="MAKER_TAKER", quantity=long_qty
    )
    logger.info(f"Long order ({decision.opportunity.long_venue}) response: {long_order_res}")

    short_order_res = short_venue_client.place_order(
        symbol=decision.short_symbol, side="SELL", order_type="MAKER_TAKER", quantity=short_qty
    )
    logger.info(f"Short order ({decision.opportunity.short_venue}) response: {short_order_res}")

    # 3. Verify positions were opened successfully.
    logger.info("Verifying positions are open and match the strategy...")
    start_time = time.time()
    timeout_seconds = 30
    while time.time() - start_time < timeout_seconds:
        try:
            hl_positions = hyperliquid_client.get_all_positions()
            aster_positions = aster_client.get_all_positions()
            if _is_portfolio_matching_opportunity(hl_positions, aster_positions, decision.opportunity):
                logger.info("Successfully verified new positions are open.")
                break

            logger.info(f"Waiting for positions to open. HL: {len(hl_positions)}, Aster: {len(aster_positions)}")
            time.sleep(2)
        except Exception as exc:
            logger.warning(f"Error during position opening verification, retrying: {exc}")
            time.sleep(2)
    else:
        logger.error(f"Timeout: Positions not confirmed open after {timeout_seconds} seconds.")
    
    logger.info("Strategy execution complete.")


def cleanup_all_open_positions_and_orders(
    aster_client: AsterClient,
    hyperliquid_client: HyperliquidClient,
    timeout_seconds: int = 900,
) -> None:
    """
    Cleans up by cancelling all open orders and closing all open positions.
    """
    logger.info("--- Starting cleanup: Cancelling all open orders and closing all positions ---")

    # 1. Cancel all open orders to prevent them from creating new positions
    logger.info("--- Cancelling open orders ---")
    try:
        aster_orders = aster_client.get_all_open_orders()
        if aster_orders:
            logger.info(f"Found {len(aster_orders)} open order(s) on Aster. Cancelling them...")
            for order in aster_orders:
                symbol = order.get("symbol")
                client_order_id = order.get("clientOrderId")
                if symbol and client_order_id:
                    try:
                        aster_client.cancel_order(symbol=symbol, orig_client_order_id=client_order_id)
                    except Exception as exc:
                        logger.error(f"Failed to cancel order {client_order_id} for {symbol} on Aster: {exc}")
        else:
            logger.info("No open orders found on Aster.")
    except Exception as exc:
        logger.error(f"Failed to get open orders from Aster: {exc}")

    try:
        hl_orders = hyperliquid_client.get_all_open_orders()
        if hl_orders:
            logger.info(f"Found {len(hl_orders)} open order(s) on Hyperliquid. Cancelling them...")
            for order in hl_orders:
                symbol = order.get("symbol")
                order_id = order.get("id")
                if symbol and order_id:
                    try:
                        hyperliquid_client.cancel_order(symbol=symbol, order_id=order_id)
                    except Exception as exc:
                        logger.error(f"Failed to cancel order {order_id} for {symbol} on Hyperliquid: {exc}")
        else:
            logger.info("No open orders found on Hyperliquid.")
    except Exception as exc:
        logger.error(f"Failed to get open orders from Hyperliquid: {exc}")

    # 2. Close all open positions
    logger.info("--- Closing open positions ---")
    try:
        aster_positions = aster_client.get_all_positions()
        if aster_positions:
            logger.info(f"Found {len(aster_positions)} open position(s) on Aster. Closing them...")
            for pos in aster_positions:
                symbol = pos.get("symbol")
                if symbol:
                    try:
                        aster_client.close_position(symbol)
                    except Exception as exc:
                        logger.error(f"Failed to close position for {symbol} on Aster: {exc}")
        else:
            logger.info("No open positions found on Aster.")
    except Exception as exc:
        logger.error(f"Failed to get positions from Aster: {exc}")

    try:
        hl_positions = hyperliquid_client.get_all_positions()
        if hl_positions:
            logger.info(f"Found {len(hl_positions)} open position(s) on Hyperliquid. Closing them...")
            for pos in hl_positions:
                symbol = pos.get("symbol")
                if symbol:
                    try:
                        hyperliquid_client.close_position(symbol)
                    except Exception as exc:
                        logger.error(f"Failed to close position for {symbol} on Hyperliquid: {exc}")
        else:
            logger.info("No open positions found on Hyperliquid.")
    except Exception as exc:
        logger.error(f"Failed to get positions from Hyperliquid: {exc}")

    # 3. Verify all positions are closed before proceeding.
    logger.info("Verifying all positions are closed...")
    start_time = time.time()
    while time.time() - start_time < timeout_seconds:
        try:
            hl_positions = hyperliquid_client.get_all_positions()
            aster_positions = aster_client.get_all_positions()
            if not hl_positions and not aster_positions:
                logger.info("Successfully verified all positions are closed.")
                break

            logger.info(f"Waiting for positions to close. HL: {len(hl_positions)}, Aster: {len(aster_positions)}")
            time.sleep(30)
        except Exception as exc:
            logger.warning(f"Error during position closure verification, retrying: {exc}")
            time.sleep(2)
    else:
        # This block runs if the while loop times out without a 'break'
        logger.error(f"Timeout: Positions not confirmed closed after {timeout_seconds} seconds.")
        # Depending on desired behavior, we could raise an exception here to halt operations.

    logger.info("--- Cleanup complete ---")
