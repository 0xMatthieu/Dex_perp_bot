"""Command-line entry point for the bot."""

from __future__ import annotations

import logging
import signal
import sys
import time
from pathlib import Path
from datetime import datetime, timedelta, timezone

# Add project root to path to allow importing from `tests`
project_root = Path(__file__).resolve().parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from decimal import Decimal

from src.dex_perp_bot.config import Settings
from src.dex_perp_bot.exchanges.aster import AsterClient
from src.dex_perp_bot.exchanges.base import DexAPIError, DexClientError
from src.dex_perp_bot.exchanges.hyperliquid import HyperliquidClient
from src.dex_perp_bot.notifier import DiscordNotifier
from src.dex_perp_bot.strategy import perform_hourly_rebalance, report_portfolio_status
from src.dex_perp_bot.status import write_status
from src.dex_perp_bot.control import CONTROL_POLL_SECONDS, read_control, write_control
from src.dex_perp_bot.strategy import cleanup_all_open_positions_and_orders
from src.dex_perp_bot.strategy import check_basis_exit, load_position_state, aster_symbol, hl_symbol, cancel_all_open_orders, reconcile_position_state
from src.dex_perp_bot.basis import BasisTracker
from src.dex_perp_bot import funding
from src.dex_perp_bot.funding import fetch_and_compare_funding_rates

STATUS_INTERVAL_SECONDS = 300

logger = logging.getLogger(__name__)


def watchlist_symbols() -> list:
    """Symbols worth sampling: last scan's candidates plus whatever we hold."""
    symbols = [o.symbol for o in funding.LAST_OPPORTUNITIES]
    state = load_position_state()
    if state and state.get("symbol") and state["symbol"] not in symbols:
        symbols.append(state["symbol"])
    return symbols


def sample_basis(aster_client, hyperliquid_client, tracker: BasisTracker) -> None:
    """Record one Aster-vs-HL basis sample per watchlist symbol (public order books only)."""
    for base in watchlist_symbols():
        try:
            a = aster_client.get_book(aster_symbol(base), depth=1)
            h = hyperliquid_client.get_book(hl_symbol(base), depth=1)
            if a["bids"] and a["asks"] and h["bids"] and h["asks"]:
                tracker.record(base, (a["bids"][0][0] + a["asks"][0][0]) / 2, (h["bids"][0][0] + h["asks"][0][0]) / 2)
        except Exception as exc:
            logger.debug("basis sample %s failed: %s", base, exc)
    tracker.save()


def handle_control(aster_client, hyperliquid_client, notifier) -> str:
    """Apply the dashboard safety switch. Returns the effective mode after handling."""
    control = read_control()
    mode = control.get("mode", "run")
    if mode == "flatten":
        logger.warning("SAFETY SWITCH: flatten requested from %s at %s. Closing everything.",
                       control.get("by"), control.get("requested_at"))
        notifier.notify_trade_closed(reason="safety switch: flatten requested from dashboard")
        try:
            cleanup_all_open_positions_and_orders(
                aster_client, hyperliquid_client, timeout_seconds=300, close_spread_ticks=1
            )
            write_control("pause", by="bot", note="flattened by bot after dashboard request")
        except Exception as exc:
            logger.exception("Flatten failed: %s", exc)
            notifier.notify_error(f"Flatten failed: {exc}")
            write_control("pause", by="bot", note=f"flatten FAILED: {exc}; check positions manually")
        return "pause"
    return mode


def main() -> int:
    """Load configuration, initialize clients, and run the delta-neutral strategy."""
    Path("logs").mkdir(exist_ok=True)
    log_filename = f"logs/bot_{datetime.now(timezone.utc).strftime('%Y-%m-%d_%H-%M-%S')}.log"
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

    notifier = DiscordNotifier(settings.discord_webhook_url)

    hyperliquid_client = HyperliquidClient(settings.hyperliquid)
    aster_client = AsterClient(settings.aster, settings.aster_config)

    # systemd stop / Ctrl+C: leave no resting order behind.
    def _on_sigterm(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, _on_sigterm)
    try:
        cancel_all_open_orders(aster_client, hyperliquid_client, reason="startup: orphans from a previous run")
        reconcile_position_state(aster_client, hyperliquid_client)
    except Exception as exc:
        logger.warning("Startup order cleanup / reconcile failed: %s", exc)

    try:
        logger.info("Synchronizing time with Aster API...")
        aster_client.sync_time()
    except DexAPIError as exc:
        logger.error("Failed to sync time with Aster: %s", exc)

    notifier.notify_startup()
    logger.info("Starting strategy loop. Press Ctrl+C to stop.")
    last_trade_hour = -1
    started_at = datetime.now(timezone.utc)
    basis_tracker = BasisTracker()
    exec_cfg = settings.execution

    def should_abort() -> bool:  # safety switch is honoured inside long executions too
        return read_control().get("mode") != "run"

    def on_tick() -> None:
        write_status(aster_client, hyperliquid_client, started_at=started_at, exec_cfg=exec_cfg, tracker=basis_tracker)
    logger.info("Execution config: %s", exec_cfg)
    try:  # seed the basis watchlist so z-scores exist by the first trading window
        fetch_and_compare_funding_rates(aster_client, hyperliquid_client, imminent_funding_minutes=60)
        sample_basis(aster_client, hyperliquid_client, basis_tracker)
    except Exception as exc:
        logger.warning("Initial scan for basis watchlist failed: %s", exc)

    try:
        while True:
            TRADE_WINDOW_START_MINUTE = 5
            TRADE_WINDOW_END_MINUTE = 55
            try:
                # Always report status on each loop iteration
                report_portfolio_status(aster_client, hyperliquid_client)

                now = datetime.now(timezone.utc)
                # Trading window is between 10 and 40 minutes past the hour.
                mode = handle_control(aster_client, hyperliquid_client, notifier)
                if mode != "run":
                    logger.info("Trading paused by safety switch (mode=%s). Holding.", mode)
                elif now.hour != last_trade_hour and TRADE_WINDOW_START_MINUTE <= now.minute <= TRADE_WINDOW_END_MINUTE:
                    last_trade_hour = now.hour
                    logger.info(f"--- Entering trading window for hour {now.hour} ---")

                    sc = settings.strategy
                    leverage = sc.leverage
                    capital_allocation_pct = Decimal(str(sc.capital_allocation_pct))
                    min_apy_diff_pct = Decimal(str(sc.min_apy_diff_pct))
                    spread_ticks = sc.spread_ticks

                    balance_hl = hyperliquid_client.get_wallet_balance().total or Decimal("0")
                    balance_aster = aster_client.get_wallet_balance().total or Decimal("0")
                    available_capital = min(balance_hl, balance_aster)
                    capital_to_deploy = available_capital * capital_allocation_pct

                    logger.info(
                        f"Available on Hyperliquid: ${balance_hl:.2f}. "
                        f"Available on Aster: ${balance_aster:.2f}. "
                        f"Min available capital: ${available_capital:.2f}. "
                        f"Allocating {capital_allocation_pct:.0%} (${capital_to_deploy:.2f}) with {leverage}x leverage."
                    )

                    if capital_to_deploy > Decimal("10"):
                        # Calculate timeout: seconds remaining until the end of the trading window.
                        end_of_window = now.replace(minute=TRADE_WINDOW_END_MINUTE, second=59, microsecond=0)
                        cleanup_timeout_seconds = (end_of_window - now).total_seconds()
                        # Ensure a minimum timeout, e.g., 60 seconds, to handle edge cases.
                        cleanup_timeout_seconds = max(60, cleanup_timeout_seconds)

                        perform_hourly_rebalance(
                            aster_client,
                            hyperliquid_client,
                            leverage=leverage,
                            capital_usd=capital_to_deploy,
                            min_apy_diff_pct=min_apy_diff_pct,
                            spread_ticks=spread_ticks,
                            cleanup_timeout_seconds=int(cleanup_timeout_seconds),
                            rebalance_hysteresis_pct=Decimal(str(sc.rebalance_hysteresis_pct)),
                            notifier=notifier,
                            exec_cfg=exec_cfg,
                            basis_tracker=basis_tracker,
                            should_abort=should_abort,
                            on_tick=on_tick,
                        )
                    else:
                        logger.warning("Insufficient capital to deploy. Awaiting next cycle.")

            except DexClientError as exc:
                logger.exception("An error occurred during the strategy execution cycle: %s", exc)
                notifier.notify_error(str(exc))

            # --- Wait until the next check/action window ---
            now = datetime.now(timezone.utc)
            # Default next run is the start of the next trading window (HH:10)
            next_run_time = now.replace(minute=TRADE_WINDOW_START_MINUTE, second=0, microsecond=0)
            if now.minute >= TRADE_WINDOW_START_MINUTE:
                # If we're already in or past this hour's window, target the next hour.
                next_run_time += timedelta(hours=1)

            wait_seconds = (next_run_time - now).total_seconds()
            # If the wait time is very short, just sleep for a default interval to avoid busy-looping
            wait_seconds = max(wait_seconds, 60)

            logger.info(f"Cycle complete. Waiting for {wait_seconds:.0f} seconds until next check around {next_run_time.strftime('%H:%M:%S')}...")
            # Sleep in chunks, refreshing the dashboard status snapshot between them.
            deadline = time.time() + wait_seconds
            last_status = 0.0
            last_sample = 0.0
            while True:
                if time.time() - last_sample >= exec_cfg.sample_interval_s:
                    try:
                        sample_basis(aster_client, hyperliquid_client, basis_tracker)
                        if read_control().get("mode") == "run" and check_basis_exit(
                            aster_client, hyperliquid_client, exec_cfg, basis_tracker, notifier
                        ):
                            last_status = 0.0  # refresh status after an exit
                    except Exception as exc:
                        logger.warning("Basis sampling / exit check failed: %s", exc)
                    last_sample = time.time()
                if time.time() - last_status >= STATUS_INTERVAL_SECONDS:
                    try:
                        write_status(aster_client, hyperliquid_client, next_window_utc=next_run_time, started_at=started_at,
                                     exec_cfg=exec_cfg, tracker=basis_tracker)
                    except Exception as exc:  # never let status reporting kill the loop
                        logger.warning("Status snapshot failed: %s", exc)
                    last_status = time.time()
                try:
                    if handle_control(aster_client, hyperliquid_client, notifier) == "pause" and read_control().get("by") == "bot":
                        last_status = 0.0  # refresh status right after a flatten
                except Exception as exc:
                    logger.warning("Control check failed: %s", exc)
                remaining = deadline - time.time()
                if remaining <= 0:
                    break
                time.sleep(min(CONTROL_POLL_SECONDS, remaining))

    except KeyboardInterrupt:
        logger.info("Shutdown signal received. Exiting.")
        try:
            cancel_all_open_orders(aster_client, hyperliquid_client, reason="shutdown")
        except Exception as exc:
            logger.warning("Shutdown order cleanup failed: %s", exc)
        notifier.notify_shutdown()
        return 0


if __name__ == "__main__":  # pragma: no cover - manual invocation entry point
    raise SystemExit(main())

