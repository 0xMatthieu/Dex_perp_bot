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
from decimal import Decimal

from src.dex_perp_bot.config import Settings
from src.dex_perp_bot.exchanges.aster import AsterClient
from src.dex_perp_bot.exchanges.base import DexAPIError, DexClientError
from src.dex_perp_bot.exchanges.hyperliquid import HyperliquidClient
from src.dex_perp_bot.strategy import determine_strategy, execute_strategy

logger = logging.getLogger(__name__)


def main() -> int:
    """Load configuration, initialize clients, and run the delta-neutral strategy."""
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

    try:
        # Determine capital from the smaller of the two available balances
        balance_hl = hyperliquid_client.get_wallet_balance().available or Decimal("0")
        balance_aster = aster_client.get_wallet_balance().available or Decimal("0")
        capital_to_deploy = min(balance_hl, balance_aster)

        if capital_to_deploy <= Decimal("10"):  # Minimum trade size check
            logger.warning(
                f"Insufficient capital to deploy strategy. "
                f"Min available balance is ${capital_to_deploy:.2f}. Needs > $10."
            )
            return 0

        # Find and execute the strategy
        decision = determine_strategy(
            aster_client, hyperliquid_client, leverage=4, capital_usd=capital_to_deploy
        )

        if decision:
            execute_strategy(aster_client, hyperliquid_client, decision)
        else:
            logger.info("No viable strategy decision was made.")

    except DexClientError as exc:
        logger.exception("An error occurred during the strategy execution: %s", exc)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry point
    raise SystemExit(main())

