"""Hyperliquid exchange connector."""

from __future__ import annotations

from typing import Any

from hyperliquid.utils import Client, LocalAccount

from .base import BalanceParsingError, DexAPIError, WalletBalance, to_decimal
from ..config import HyperliquidCredentials


class HyperliquidClient:
    """Wrapper around the official Hyperliquid SDK."""

    def __init__(
        self,
        credentials: HyperliquidCredentials,
    ) -> None:
        self._credentials = credentials
        account = LocalAccount.from_key(credentials.private_key)
        self._client = Client(account)

    def get_wallet_balance(self) -> WalletBalance:
        """Return the wallet balance reported by Hyperliquid."""

        try:
            balance = self._client.get_balance()
        except Exception as exc:  # pragma: no cover - defensive
            raise DexAPIError("Failed to fetch Hyperliquid balance") from exc

        usdc = balance.get("USDC", {}) if isinstance(balance, dict) else {}
        total = to_decimal(usdc.get("total")) if isinstance(usdc, dict) else None
        available = to_decimal(usdc.get("free")) if isinstance(usdc, dict) else None

        if total is None and available is None:
            raise BalanceParsingError("Hyperliquid balance response missing USDC totals")

        return WalletBalance(total=total, available=available, raw=balance)

