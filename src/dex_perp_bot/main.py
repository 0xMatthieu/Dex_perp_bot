"""Command-line entry point for the bot."""

from __future__ import annotations

import logging
import sys
from pathlib import Path

# Add project root to path to allow importing from `tests`
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from src.dex_perp_bot.config import Settings
from src.dex_perp_bot.exchanges.aster import AsterClient
from src.dex_perp_bot.exchanges.base import DexAPIError
from src.dex_perp_bot.exchanges.hyperliquid import HyperliquidClient
from tests.test import run_funding_test

logger = logging.getLogger(__name__)


def main() -> int:
    """Load configuration, initialize clients, and run tests."""
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
        logger.error("Failed to sync time with Aster: %s", exc)
        # It's not critical for funding tests, but good to know.

    # As requested, run only the funding test from the main entry point.
    run_funding_test(aster_client, hyperliquid_client)

    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry point
    raise SystemExit(main())

