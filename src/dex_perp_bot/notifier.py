"""Optional Discord webhook notifier for critical bot events."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from decimal import Decimal
from typing import Optional

import requests

logger = logging.getLogger(__name__)


class DiscordNotifier:
    """Sends messages to a Discord channel via webhook. No-ops if not configured."""

    def __init__(self, webhook_url: Optional[str] = None) -> None:
        self._webhook_url = webhook_url
        if self._webhook_url:
            logger.info("Discord notifications enabled.")
        else:
            logger.info("Discord notifications disabled (no webhook URL).")

    @property
    def enabled(self) -> bool:
        return bool(self._webhook_url)

    def _send(self, content: str) -> None:
        if not self._webhook_url:
            return
        try:
            resp = requests.post(
                self._webhook_url,
                json={"content": content},
                timeout=10,
            )
            if resp.status_code not in (200, 204):
                logger.warning("Discord webhook returned %s: %s", resp.status_code, resp.text[:200])
        except requests.RequestException as exc:
            logger.warning("Failed to send Discord notification: %s", exc)

    def _ts(self) -> str:
        return datetime.now(timezone.utc).strftime("%H:%M UTC")

    # ------------------------------------------------------------------
    # Event methods
    # ------------------------------------------------------------------

    def notify_trade_opened(
        self,
        symbol: str,
        long_venue: str,
        short_venue: str,
        apy_difference: Decimal,
        leverage: int,
        capital: Decimal,
    ) -> None:
        self._send(
            f":arrows_counterclockwise: **Position Opened** ({self._ts()})\n"
            f"**{symbol}** — Long {long_venue} / Short {short_venue}\n"
            f"APY diff: **{apy_difference:.2f}%** | {leverage}x leverage | ${capital:,.2f} deployed"
        )

    def notify_trade_closed(self, reason: str = "rebalance") -> None:
        self._send(
            f":white_check_mark: **Positions Closed** ({self._ts()}) — {reason}"
        )

    def notify_holding(self, symbol: str, apy_difference: Decimal) -> None:
        self._send(
            f":pause_button: **Holding** {symbol} ({self._ts()}) — APY diff: {apy_difference:.2f}%"
        )

    def notify_no_opportunity(self, min_apy: Decimal) -> None:
        self._send(
            f":zzz: No opportunity above {min_apy:.0f}% APY threshold ({self._ts()})"
        )

    def notify_rollback(self, reason: str) -> None:
        self._send(
            f":warning: **ROLLBACK** ({self._ts()})\n{reason}"
        )

    def notify_error(self, error: str) -> None:
        self._send(
            f":x: **Error** ({self._ts()})\n```\n{error[:1500]}\n```"
        )

    def notify_startup(self) -> None:
        self._send(f":rocket: **Bot started** ({self._ts()})")

    def notify_shutdown(self) -> None:
        self._send(f":stop_sign: **Bot stopped** ({self._ts()})")
