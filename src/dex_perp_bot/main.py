"""Command-line entry point for the bot."""

from __future__ import annotations

import logging
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta

# Add project root to path to allow importing from `tests`
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from decimal import Decimal

from src.dex_perp_bot.config import Settings
from src.dex_perp_bot.exchanges.aster import AsterClient
from src.dex_perp_bot.exchanges.base import DexAPIError, DexClientError
from src.dex_perp_bot.exchanges.hyperliquid import HyperliquidClient
from src.dex_perp_bot.strategy import perform_hourly_rebalance, report_portfolio_status

logger = logging.getLogger(__name__)


def main() -> int:
    """Load configuration, initialize clients, and run the delta-neutral strategy."""
    log_filename = f"logs/bot_{datetime.now().strftime('%Y-%m-%d_%H-%M-%S')}.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=[
            logging.FileHandler(log_filename),
            logging.StreamHandler(sys.stdout),
        ],
    )

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

    logger.info("Starting strategy loop. Press Ctrl+C to stop.")
    last_trade_hour = -1

    try:
        while True:
            try:
                # Always report status on each loop iteration
                report_portfolio_status(aster_client, hyperliquid_client)

                now = datetime.now()
                # Trading window is between 10 and 40 minutes past the hour.
                if now.hour != last_trade_hour and 10 <= now.minute <= 40:
                    last_trade_hour = now.hour
                    logger.info(f"--- Entering trading window for hour {now.hour} ---")

                    leverage = 4
                    capital_allocation_pct = Decimal("0.9")
                    min_apy_diff_pct = Decimal("0")  # Minimum APY difference to consider a trade
                    min_spread_pct = Decimal("0")  # Minimum price spread to enter a trade

                    balance_hl = hyperliquid_client.get_wallet_balance().available or Decimal("0")
                    balance_aster = aster_client.get_wallet_balance().available or Decimal("0")
                    available_capital = min(balance_hl, balance_aster)
                    capital_to_deploy = available_capital * capital_allocation_pct

                    logger.info(
                        f"Available on Hyperliquid: ${balance_hl:.2f}. "
                        f"Available on Aster: ${balance_aster:.2f}. "
                        f"Min available capital: ${available_capital:.2f}. "
                        f"Allocating {capital_allocation_pct:.0%} (${capital_to_deploy:.2f}) with {leverage}x leverage."
                    )

                    if capital_to_deploy > Decimal("10"):
                        perform_hourly_rebalance(
                            aster_client,
                            hyperliquid_client,
                            leverage=leverage,
                            capital_usd=capital_to_deploy,
                            min_apy_diff_pct=min_apy_diff_pct,
                            min_spread_pct=min_spread_pct,
                        )
                    else:
                        logger.warning("Insufficient capital to deploy. Awaiting next cycle.")

            except DexClientError as exc:
                logger.exception("An error occurred during the strategy execution cycle: %s", exc)

            # --- Wait until the next check/action window ---
            now = datetime.now()
            # Default next run is the start of the next trading window (HH:10)
            next_run_time = now.replace(minute=10, second=0, microsecond=0)
            if now.minute >= 10:
                # If we're already in or past this hour's window, target the next hour.
                next_run_time += timedelta(hours=1)

            wait_seconds = (next_run_time - now).total_seconds()
            # If the wait time is very short, just sleep for a default interval to avoid busy-looping
            wait_seconds = max(wait_seconds, 60)

            logger.info(f"Cycle complete. Waiting for {wait_seconds:.0f} seconds until next check around {next_run_time.strftime('%H:%M:%S')}...")
            time.sleep(wait_seconds)

    except KeyboardInterrupt:
        logger.info("Shutdown signal received. Exiting.")
        return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry point
    raise SystemExit(main())

