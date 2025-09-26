"""Hyperliquid exchange connector."""

from __future__ import annotations

from typing import Any, Callable, Dict

from hyperliquid import HyperliquidSync

from .base import BalanceParsingError, DexAPIError, WalletBalance, to_decimal
from ..config import HyperliquidCredentials


ClientFactory = Callable[[Dict[str, Any]], Any]


class HyperliquidClient:
    """Wrapper around the official Hyperliquid CCXT connector."""

    def __init__(
        self,
        credentials: HyperliquidCredentials,
        *,
        client_factory: ClientFactory | None = None,
    ) -> None:
        self._credentials = credentials
        factory = client_factory or HyperliquidSync
        self._client = factory(
            {
                "privateKey": credentials.private_key,
                "walletAddress": credentials.wallet_address,
            }
        )

    def get_wallet_balance(self) -> WalletBalance:
        """Return the wallet balance reported by Hyperliquid."""

        try:
            balance = self._client.fetch_balance()
        except Exception as exc:  # pragma: no cover - defensive
            raise DexAPIError("Failed to fetch Hyperliquid balance") from exc

        usdc = balance.get("USDC", {}) if isinstance(balance, dict) else {}
        total = to_decimal(usdc.get("total")) if isinstance(usdc, dict) else None
        available = to_decimal(usdc.get("free")) if isinstance(usdc, dict) else None

        if total is None and available is None:
            raise BalanceParsingError("Hyperliquid balance response missing USDC totals")

        return WalletBalance(total=total, available=available, raw=balance)

