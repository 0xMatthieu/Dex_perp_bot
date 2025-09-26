"""Hyperliquid exchange connector."""

from __future__ import annotations

from typing import Any

from hyperliquid.info import Info
from hyperliquid.utils.constants import MAINNET_API_URL
from hyperliquid.utils.signing import LocalAccount

from .base import BalanceParsingError, DexAPIError, WalletBalance, to_decimal
from ..config import HyperliquidCredentials


class HyperliquidClient:
    """Wrapper around the official Hyperliquid SDK."""

    def __init__(
        self,
        credentials: HyperliquidCredentials,
    ) -> None:
        self._credentials = credentials
        self._account = LocalAccount.from_key(credentials.private_key)
        self._info = Info(MAINNET_API_URL, skip_ws=True)

    def get_wallet_balance(self) -> WalletBalance:
        """Return the wallet balance reported by Hyperliquid."""

        try:
            balance = self._info.user_state(self._account.address)
        except Exception as exc:  # pragma: no cover - defensive
            raise DexAPIError("Failed to fetch Hyperliquid balance") from exc

        margin_summary = balance.get("marginSummary") if isinstance(balance, dict) else None
        if not isinstance(margin_summary, dict):
            raise BalanceParsingError("Hyperliquid balance response missing marginSummary")

        total = to_decimal(margin_summary.get("accountValue"))

        # Available balance is total account value less margin used
        margin_used = to_decimal(margin_summary.get("totalMarginUsed"))
        available = None
        if total is not None and margin_used is not None:
            available = total - margin_used

        if total is None:
            raise BalanceParsingError("Hyperliquid balance response missing accountValue")

        return WalletBalance(total=total, available=available, raw=balance)

