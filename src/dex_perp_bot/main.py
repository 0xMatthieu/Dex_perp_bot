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
from src.dex_perp_bot.strategy import cleanup_all_open_positions_and_orders, run_arbitrage_strategy

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
        leverage = 4
        # Use 100% of the available capital. To be more conservative, set this below 1.0.
        capital_allocation_pct = Decimal("1.0")

        # Determine capital from the smaller of the two available balances
        balance_hl = hyperliquid_client.get_wallet_balance().available or Decimal("0")
        balance_aster = aster_client.get_wallet_balance().available or Decimal("0")
        available_capital = min(balance_hl, balance_aster)
        capital_to_deploy = available_capital * capital_allocation_pct

        logger.info(
            f"Available capital (min across exchanges): ${available_capital:.2f}. "
            f"Allocating {capital_allocation_pct:.0%} (${capital_to_deploy:.2f}) with {leverage}x leverage."
        )
        notional_position_size = capital_to_deploy * Decimal(leverage)
        logger.info(f"Target notional position size per leg: ${notional_position_size:.2f}")

        if capital_to_deploy <= Decimal("10"):  # Minimum trade size check
            logger.warning(
                f"Insufficient capital to deploy strategy. "
                f"Allocated capital is ${capital_to_deploy:.2f}, which is below the $10 minimum."
            )
            return 0

        # Run the arbitrage strategy, which will handle rebalancing internally.
        run_arbitrage_strategy(
            aster_client, hyperliquid_client, leverage=leverage, capital_usd=capital_to_deploy
        )

    except DexClientError as exc:
        logger.exception("An error occurred during the strategy execution: %s", exc)
        return 1

    return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry point
    raise SystemExit(main())

