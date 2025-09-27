"""Command-line entry point for querying exchange balances."""

from __future__ import annotations

import json
import logging
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
        logger.info("Querying Aster funding rate for ETH-PERP...")
        funding_rates = aster_client.get_funding_rate(symbol=None, limit=5)
        summary["aster_funding"] = funding_rates
    except DexClientError as exc:
        logger.exception("Failed to query Aster funding rates")
        summary["aster_funding"] = {"error": str(exc)}

    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry point
    raise SystemExit(main())

