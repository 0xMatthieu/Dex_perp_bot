"""Aster exchange connector."""

from __future__ import annotations

import hashlib
import hmac
import time
import urllib.parse
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

import requests

from .base import (
    BalanceParsingError,
    DexAPIError,
    WalletBalance,
    find_first_key,
    get_from_path,
    to_decimal,
)
from ..config import AsterConfig, AsterCredentials

KeyVals = Sequence[Tuple[str, Any]]

class AsterClient:
    """HTTP client for interacting with the Aster API (fapi.*)."""

    def __init__(
        self,
        credentials: AsterCredentials,
        config: AsterConfig,
        *,
        session: Optional[requests.Session] = None,
    ) -> None:
        self._credentials = credentials
        self._config = config
        self._session = session or requests.Session()
        self._time_offset_ms = 0  # server - local

    def sync_time(self) -> None:
        """GET /fapi/v1/time and cache the offset (serverTime - now)."""
        url = f"{self._config.base_url.rstrip('/')}/fapi/v1/time"
        r = self._session.get(url, timeout=self._config.request_timeout)
        r.raise_for_status()
        server_time = int(r.json()["serverTime"])
        now = int(time.time() * 1000)
        self._time_offset_ms = server_time - now

    def _urlencode(self, items: KeyVals) -> str:
        # EXACT encoding used to sign & send
        return urllib.parse.urlencode(items, doseq=True)

    def _sign(self, total_params: str) -> str:
        secret = self._credentials.api_secret.encode("utf-8")
        return hmac.new(secret, total_params.encode("utf-8"), hashlib.sha256).hexdigest()

    def _headers(self) -> Dict[str, str]:
        return {"X-MBX-APIKEY": self._credentials.api_key}

    def get_wallet_balance(self) -> WalletBalance:
        """
        Fetch account info and return parsed balance.

        Uses GET /fapi/v4/account (SIGNED):
        - total fields: totalWalletBalance / totalMarginBalance
        - available fields: availableBalance / maxWithdrawAmount / totalMarginBalance
        """
        # Aster SIGNED endpoints require timestamp (ms) and signature over the query string.
        params: Dict[str, Any] = {
            "timestamp": int(time.time() * 1000) + self._time_offset_ms,
            # optional but recommended to mitigate drift
            "recvWindow": 5000,
        }

        response = self._get_signed(self._config.balance_endpoint, params)
        account_data = self._extract_account_data(response)

        total = to_decimal(find_first_key(account_data, self._config.total_fields))
        available = to_decimal(find_first_key(account_data, self._config.available_fields))

        if total is None and available is None:
            raise BalanceParsingError(
                "Aster account payload missing both total and available fields"
            )

        return WalletBalance(total=total, available=available, raw=response)

    # ------------------------------------------------------------------
    # Internal helpers

    def _get_signed(self, endpoint: str, params: Mapping[str, Any]) -> Mapping[str, Any]:
        """
        Signed GET per Aster docs:
        - Build query string in the exact order you send
        - signature = HMAC_SHA256(secretKey, queryString)
        - Header: X-MBX-APIKEY
        """
        url = f"{self._config.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        query = urllib.parse.urlencode(params, doseq=True)
        signature = self._sign(query)

        headers = {"X-MBX-APIKEY": self._credentials.api_key}
        full_params = dict(params)
        full_params["signature"] = signature

        try:
            r = self._session.get(
                url, params=full_params, headers=headers, timeout=self._config.request_timeout
            )
            r.raise_for_status()
        except requests.RequestException as exc:  # pragma: no cover
            raise DexAPIError(f"Aster request failed: {exc}") from exc

        try:
            return r.json()
        except ValueError as exc:  # pragma: no cover
            raise DexAPIError("Failed to decode JSON response from Aster") from exc

    def _sign(self, query_string: str) -> str:
        secret = self._credentials.api_secret.encode("utf-8")
        return hmac.new(secret, query_string.encode("utf-8"), hashlib.sha256).hexdigest()

    def _extract_account_data(self, payload: Mapping[str, Any]) -> Mapping[str, Any]:
        # If response_path is empty, use the whole payload (v4/account is top-level)
        if not self._config.response_path:
            if not isinstance(payload, Mapping):
                raise BalanceParsingError("Aster response is not a JSON object")
            return payload

        account_data = get_from_path(payload, self._config.response_path)
        if not isinstance(account_data, Mapping):
            raise BalanceParsingError("Aster response did not contain expected account data")
        return account_data
