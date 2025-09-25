"""Aster exchange connector."""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any, Mapping, Optional

import requests

from .base import (
    BalanceParsingError,
    DexAPIError,
    WalletBalance,
    find_first_key,
    get_from_path,
    to_decimal,
)
from ..config import AsterCredentials


class AsterClient:
    """HTTP client for interacting with the Aster API."""

    def __init__(
        self,
        credentials: AsterCredentials,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._credentials = credentials
        self._session = session or requests.Session()

    def get_wallet_balance(self) -> WalletBalance:
        """Fetch the account summary and return the parsed balance."""

        payload = {"account": self._credentials.account_id}
        response = self._post(self._credentials.balance_endpoint, payload)
        account_data = self._extract_account_data(response)

        total = to_decimal(find_first_key(account_data, self._credentials.total_fields))
        available = to_decimal(find_first_key(account_data, self._credentials.available_fields))

        if total is None and available is None:
            raise BalanceParsingError("Aster balance payload missing total and available fields")

        return WalletBalance(total=total, available=available, raw=response)

    # ------------------------------------------------------------------
    # Internal helpers

    def _post(self, endpoint: str, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        url = f"{self._credentials.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        body = json.dumps(payload, separators=(",", ":"))
        timestamp = str(int(time.time() * 1000))
        signature = self._sign(timestamp, body)

        headers = {
            "Content-Type": "application/json",
            "X-API-KEY": self._credentials.api_key,
            "X-TIMESTAMP": timestamp,
            "X-SIGNATURE": signature,
        }

        try:
            response = self._session.post(
                url,
                data=body,
                headers=headers,
                timeout=self._credentials.request_timeout,
            )
            response.raise_for_status()
        except requests.RequestException as exc:  # pragma: no cover - defensive
            raise DexAPIError(f"Aster request failed: {exc}") from exc

        try:
            return response.json()
        except ValueError as exc:  # pragma: no cover - defensive
            raise DexAPIError("Failed to decode JSON response from Aster") from exc

    def _sign(self, timestamp: str, body: str) -> str:
        message = f"{timestamp}{body}"
        secret = self._credentials.api_secret.encode("utf-8")
        return hmac.new(secret, message.encode("utf-8"), hashlib.sha256).hexdigest()

    def _extract_account_data(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        account_data = get_from_path(payload, self._credentials.response_path)
        if not isinstance(account_data, Mapping):
            raise BalanceParsingError("Aster response did not contain account summary data")
        return account_data

