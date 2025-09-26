"""Hyperliquid exchange connector."""

from __future__ import annotations

from typing import Any, Optional

import requests
from hyperliquid.utils.signing import LocalAccount

from .base import BalanceParsingError, DexAPIError, WalletBalance, to_decimal
from ..config import HyperliquidCredentials


class HyperliquidClient:
    """Wrapper around the official Hyperliquid SDK."""

    def __init__(
        self,
        credentials: HyperliquidCredentials,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._credentials = credentials
        self._account = LocalAccount.from_key(credentials.private_key)
        self._info_url = "https://api.hyperliquid.xyz/info"
        self._session = session or requests.Session()

    def get_wallet_balance(self) -> WalletBalance:
        """Return the wallet balance reported by Hyperliquid."""

        payload = {
            "type": "userState",
            "user": self._account.address,
        }
        try:
            response = self._session.post(self._info_url, json=payload, timeout=10)
            response.raise_for_status()
            balance = response.json()
        except requests.RequestException as exc:  # pragma: no cover - defensive
            raise DexAPIError("Failed to fetch Hyperliquid balance") from exc
        except ValueError as exc:  # pragma: no cover - defensive
            raise DexAPIError("Failed to decode JSON response from Hyperliquid") from exc

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

