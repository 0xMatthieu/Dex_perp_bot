"""Command-line entry point for querying exchange balances."""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict

from .config import Settings
from .exchanges.aster import AsterClient
from .exchanges.base import DexClientError, DexAPIError
from .exchanges.hyperliquid import HyperliquidClient

logger = logging.getLogger(__name__)


def main() -> int:
    """Load configuration, query balances, and print a summary."""

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    try:
        settings = Settings.from_env()
    except ValueError as exc:
        logger.error("Configuration error: %s", exc)
        return 1

    hyperliquid_client = HyperliquidClient(settings.hyperliquid)
    aster_client = AsterClient(settings.aster, settings.aster_config)

    try:
        logger.info("Synchronizing time with Aster API...")
        aster_client.sync_time()
    except DexAPIError as exc:
        logger.error("Failed to sync time with Aster, balance query will likely fail: %s", exc)

    summary: Dict[str, Any] = {}

    for venue, client in ("hyperliquid", hyperliquid_client), ("aster", aster_client):
        try:
            balance = client.get_wallet_balance()
        except DexClientError as exc:
            logger.exception("Failed to query %s balance", venue)
            summary[venue] = {"error": str(exc)}
        #else:
        #    summary[venue] = balance.as_dict()

    try:
        logger.info("Querying Aster funding rate for all symbol...")
        funding_rates = aster_client.get_funding_rate(symbol=None, limit=None)
        summary["aster_funding"] = funding_rates
    except DexClientError as exc:
        logger.exception("Failed to query Aster funding rates")
        #summary["aster_funding"] = {"error": str(exc)}

    try:
        logger.info("Querying Hyperliquid predicted funding rates...")
        predicted_rates = hyperliquid_client.get_predicted_funding_rates()
        summary["hyperliquid_predicted_funding"] = predicted_rates
    except DexClientError as exc:
        logger.exception("Failed to query Hyperliquid predicted funding rates")
        summary["hyperliquid_predicted_funding"] = {"error": str(exc)}

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

    #print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry point
    raise SystemExit(main())

