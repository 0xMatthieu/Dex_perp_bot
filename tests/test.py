from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, TYPE_CHECKING

from src.dex_perp_bot.exchanges.base import DexClientError
from src.dex_perp_bot.funding import fetch_and_compare_funding_rates

if TYPE_CHECKING:
    from src.dex_perp_bot.exchanges.aster import AsterClient
    from src.dex_perp_bot.exchanges.hyperliquid import HyperliquidClient

logger = logging.getLogger(__name__)


def run_wallet_balance_test(aster_client: AsterClient, hyperliquid_client: HyperliquidClient) -> Dict[str, Any]:
    """Runs a wallet balance test against Aster and Hyperliquid."""
    summary: Dict[str, Any] = {}
    for venue, client in ("hyperliquid", hyperliquid_client), ("aster", aster_client):
        try:
            balance = client.get_wallet_balance()
            summary[venue] = balance.as_dict()
        except DexClientError as exc:
            logger.exception("Failed to query %s balance", venue)
            summary[venue] = {"error": str(exc)}

    print("\n--- Wallet Balance Test Results ---")
    print(json.dumps(summary, indent=2))
    return summary


def run_funding_test(aster_client: AsterClient, hyperliquid_client: HyperliquidClient) -> Dict[str, Any]:
    """Runs a funding rate comparison test."""
    summary: Dict[str, Any] = {}
    try:
        top_funding_pairs = fetch_and_compare_funding_rates(aster_client, hyperliquid_client)
        summary["top_funding_opportunities"] = [str(p) for p in top_funding_pairs]
    except DexClientError as exc:
        logger.exception("Failed to fetch or compare funding rates")
        summary["top_funding_opportunities"] = {"error": str(exc)}

    print("\n--- Funding Test Results ---")
    print(json.dumps(summary, indent=2))
    return summary


def run_order_tests(aster_client: AsterClient, hyperliquid_client: HyperliquidClient) -> Dict[str, Any]:
    """Runs a sequence of order management tests against Aster and Hyperliquid."""
    summary: Dict[str, Any] = {}

    # --- Aster Tests ---
    # Test 1: Market order -> should be filled -> logic should close the position.
    try:
        logger.info("--- Aster Test 1: Market Order -> Close Position ---")
        market_order = aster_client.create_order(
            side="BUY", order_type="MARKET", leverage=10, margin_usd=20.0
        )
        summary["aster_market_order"] = market_order

        if "clientOrderId" in market_order:
            client_order_id = market_order["clientOrderId"]
            symbol = market_order.get("symbol", "BTCUSDT")
            logger.info("Market order sent for %s. Waiting 10s before closing position.", symbol)
            time.sleep(10)
            close_response = aster_client.cancel_or_close(symbol=symbol, client_order_id=client_order_id)
            summary["aster_market_order_close"] = close_response
            logger.info("Close position request sent for %s.", symbol)
        else:
            logger.warning("Aster market order did not return a clientOrderId.")

    except (DexClientError, ValueError) as exc:
        logger.exception("Aster Test 1 failed")
        summary["aster_market_order"] = {"error": str(exc)}

    # Test 2: Limit order (far) -> should be open -> logic should cancel it.
    try:
        logger.info("--- Aster Test 2: Limit Order -> Cancel Order ---")
        limit_order = aster_client.create_order(
            side="BUY", order_type="LIMIT", leverage=10, margin_usd=20.0
        )
        summary["aster_limit_order"] = limit_order

        if "clientOrderId" in limit_order:
            client_order_id = limit_order["clientOrderId"]
            symbol = limit_order.get("symbol", "BTCUSDT")
            logger.info("Limit order sent for %s. Waiting 10s before cancelling.", symbol)
            time.sleep(10)
            cancel_response = aster_client.cancel_or_close(symbol=symbol, client_order_id=client_order_id)
            summary["aster_limit_order_cancel"] = cancel_response
            logger.info("Cancel order request sent for %s.", symbol)
        else:
            logger.warning("Aster limit order did not return a clientOrderId.")

    except (DexClientError, ValueError) as exc:
        logger.exception("Aster Test 2 failed")
        summary["aster_limit_order"] = {"error": str(exc)}

    # --- Hyperliquid Tests ---
    # Test 1: Market order -> should be filled -> logic should close the position.
    try:
        logger.info("--- Hyperliquid Test 1: Market Order -> Close Position ---")
        market_order = hyperliquid_client.create_order(
            side="BUY", order_type="MARKET", leverage=10, margin_usd=20.0
        )
        summary["hyperliquid_market_order"] = market_order

        if "id" in market_order:
            order_id = market_order["id"]
            symbol = "BTC/USDC:USDC"
            logger.info("Market order sent for %s. Waiting 10s before closing position.", symbol)
            time.sleep(10)
            close_response = hyperliquid_client.cancel_or_close(symbol=symbol, order_id=order_id)
            summary["hyperliquid_market_order_close"] = close_response
            logger.info("Close position request sent for %s.", symbol)
        else:
            logger.warning("Hyperliquid market order did not return an id.")

    except (DexClientError, ValueError) as exc:
        logger.exception("Hyperliquid Test 1 failed")
        summary["hyperliquid_market_order"] = {"error": str(exc)}

    # Test 2: Limit order (far) -> should be open -> logic should cancel it.
    try:
        logger.info("--- Hyperliquid Test 2: Limit Order -> Cancel Order ---")
        limit_order = hyperliquid_client.create_order(
            side="BUY", order_type="LIMIT", leverage=10, margin_usd=20.0
        )
        summary["hyperliquid_limit_order"] = limit_order

        if "id" in limit_order:
            order_id = limit_order["id"]
            symbol = "BTC/USDC:USDC"
            logger.info("Limit order sent for %s. Waiting 10s before cancelling.", symbol)
            time.sleep(10)
            cancel_response = hyperliquid_client.cancel_or_close(symbol=symbol, order_id=order_id)
            summary["hyperliquid_limit_order_cancel"] = cancel_response
            logger.info("Cancel order request sent for %s.", symbol)
        else:
            logger.warning("Hyperliquid limit order did not return an id.")

    except (DexClientError, ValueError) as exc:
        logger.exception("Hyperliquid Test 2 failed")
        summary["hyperliquid_limit_order"] = {"error": str(exc)}

    print("\n--- Order Test Results ---")
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    import sys
    from pathlib import Path

    # Add project root to path to allow importing from `src`
    project_root = Path(__file__).resolve().parent.parent
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))

    from src.dex_perp_bot.config import Settings
    from src.dex_perp_bot.exchanges.aster import AsterClient
    from src.dex_perp_bot.exchanges.hyperliquid import HyperliquidClient
    from src.dex_perp_bot.exchanges.base import DexAPIError

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        sys.exit(1)

    hyperliquid_client = HyperliquidClient(settings.hyperliquid)
    aster_client = AsterClient(settings.aster, settings.aster_config)

    try:
        logger.info("Synchronizing time with Aster API...")
        aster_client.sync_time()
    except DexAPIError as exc:
        logger.error("Failed to sync time with Aster: %s", exc)
        # It's not critical for funding tests, but good to know.

    run_funding_test(aster_client, hyperliquid_client)
