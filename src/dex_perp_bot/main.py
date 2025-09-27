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

    try:
        logger.info("Creating a test order on Aster...")
        # Example: 10x leverage on 20 USD margin for a LIMIT BUY order.
        order_response = aster_client.create_order(
            side="BUY",
            order_type="LIMIT",
            leverage=10,
            margin_usd=20.0,
        )
        summary["aster_order"] = order_response

        # If order was created successfully, wait 10s then cancel it for this test.
        if "orderId" in order_response and isinstance(order_response.get("orderId"), int):
            order_id = order_response["orderId"]
            symbol = order_response.get("symbol", "BTCUSDT")  # Fallback to what we know we used
            logger.info("Successfully created order %s for %s, waiting 10s before cancelling.", order_id, symbol)
            time.sleep(10)
            logger.info("Now cancelling order %s.", order_id)
            try:
                cancel_response = aster_client.cancel_order(symbol=symbol, order_id=order_id)
                summary["aster_cancel_order"] = cancel_response
                logger.info("Successfully cancelled order %s.", order_id)
            except DexClientError as exc_cancel:
                logger.exception("Failed to cancel order on Aster")
                summary["aster_cancel_order"] = {"error": str(exc_cancel)}
        else:
            logger.warning("Order creation did not return an orderId, skipping cancellation.")

    except (DexClientError, ValueError) as exc:
        logger.exception("Failed to create order on Aster")
        summary["aster_order"] = {"error": str(exc)}

    try:
        logger.info("Creating a test order on Hyperliquid...")
        # Example: 10x leverage on 20 USD margin for a LIMIT BUY order.
        hl_order_response = hyperliquid_client.create_order(
            side="BUY",
            order_type="LIMIT",
            leverage=10,
            margin_usd=20.0,
        )
        summary["hyperliquid_order"] = hl_order_response

        # If order was created successfully, wait 10s then cancel it for this test.
        if "id" in hl_order_response and isinstance(hl_order_response.get("id"), str):
            order_id = hl_order_response["id"]
            symbol = hl_order_response.get("symbol")
            if not symbol:
                # ccxt's Hyperliquid order creation response might not include the symbol.
                # We'll use the one we know we passed to create_order for cancellation.
                symbol = "BTC/USDC:USDC"
                logger.warning(
                    "Hyperliquid order response missing 'symbol', using '%s' for cancellation.",
                    symbol,
                )

            logger.info("Successfully created Hyperliquid order %s for %s, waiting 10s before cancelling.", order_id, symbol)
            time.sleep(10)
            logger.info("Now cancelling Hyperliquid order %s.", order_id)
            try:
                cancel_response = hyperliquid_client.cancel_order(symbol=symbol, order_id=order_id)
                summary["hyperliquid_cancel_order"] = cancel_response
                logger.info("Successfully cancelled Hyperliquid order %s.", order_id)
            except DexClientError as exc_cancel:
                logger.exception("Failed to cancel order on Hyperliquid")
                summary["hyperliquid_cancel_order"] = {"error": str(exc_cancel)}
        else:
            logger.warning("Hyperliquid order creation did not return an id, skipping cancellation.")

    except (DexClientError, ValueError) as exc:
        logger.exception("Failed to create order on Hyperliquid")
        summary["hyperliquid_order"] = {"error": str(exc)}

    #print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry point
    raise SystemExit(main())

