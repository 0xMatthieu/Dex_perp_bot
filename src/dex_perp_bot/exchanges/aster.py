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
        self._time_offset_ms = 0  # serverTime - local

    # -------- Time sync (fix -1021) --------
    def sync_time(self) -> None:
        """GET /fapi/v1/time and cache the offset (serverTime - now)."""
        url = f"{self._config.base_url.rstrip('/')}/fapi/v1/time"
        try:
            r = self._session.get(url, timeout=self._config.request_timeout)
            r.raise_for_status()
            server = int(r.json()["serverTime"])
            self._time_offset_ms = server - int(time.time() * 1000)
        except (requests.RequestException, ValueError) as exc:
            raise DexAPIError("Failed to sync time with Aster") from exc

    def _now_ms(self) -> int:
        return int(time.time() * 1000) + self._time_offset_ms

    # -------- Signing primitives (Binance-style) --------
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
        response = self._get_signed(self._config.balance_endpoint, params=[])
        account_data = self._extract_account_data(response)

        total = to_decimal(find_first_key(account_data, self._config.total_fields))
        available = to_decimal(find_first_key(account_data, self._config.available_fields))

        if total is None and available is None:
            raise BalanceParsingError(
                "Aster account payload missing both total and available fields"
            )

        return WalletBalance(total=total, available=available, raw=response)

    # -------- GET SIGNED (query only) --------
    def _get_signed(self, endpoint: str, params: Optional[KeyVals] = None) -> Mapping[str, Any]:
        base_items: List[Tuple[str, Any]] = []
        # Put recvWindow first or last — order must match what you sign & send. We keep it first.
        base_items.append(("recvWindow", 5000))
        base_items.append(("timestamp", self._now_ms()))
        if params:
            base_items.extend(list(params))  # preserve caller order

        query_str = self._urlencode(base_items)
        sig = self._sign(query_str)

        # signature MUST be last
        url = f"{self._config.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        full_query = f"{query_str}&signature={sig}"

        r = self._session.get(f"{url}?{full_query}", headers=self._headers(),
                              timeout=self._config.request_timeout)
        self._raise_for_json(r)
        return r.json()

    # -------- POST SIGNED (supports body, query, or mixed per docs) --------
    def _post_signed(
        self,
        endpoint: str,
        *,
        query: Optional[KeyVals] = None,
        body: Optional[KeyVals] = None,
        signature_location: str = "body",  # "body" | "query"
    ) -> Mapping[str, Any]:
        """
        Implements: totalParams = urlencode(query) + ('&' if both) + urlencode(body)
        Signs totalParams. Sends url-encoded (not JSON) like Binance/Aster examples.
        Ensures signature is the LAST param in the chosen location.
        """
        q_items: List[Tuple[str, Any]] = [("recvWindow", 5000), ("timestamp", self._now_ms())]
        if query:
            q_items.extend(list(query))

        b_items: List[Tuple[str, Any]] = []
        if body:
            b_items.extend(list(body))

        query_str = self._urlencode(q_items)
        body_str = self._urlencode(b_items) if b_items else ""

        if query_str and body_str:
            total_params = f"{query_str}&{body_str}"
        else:
            total_params = query_str or body_str  # exactly one side

        signature = self._sign(total_params)

        url = f"{self._config.base_url.rstrip('/')}/{endpoint.lstrip('/')}"
        headers = {
            **self._headers(),
            "Content-Type": "application/x-www-form-urlencoded",
        }

        if signature_location == "query":
            # signature last in query
            q_to_send = query_str + ("&signature=" + signature if query_str else "signature=" + signature)
            r = self._session.post(
                f"{url}?{q_to_send}",
                data=body_str,  # may be empty; still form-encoded
                headers=headers,
                timeout=self._config.request_timeout,
            )
        else:
            # signature last in body
            if body_str:
                body_to_send = f"{body_str}&signature={signature}"
            else:
                body_to_send = f"signature={signature}"
            r = self._session.post(
                f"{url}?{query_str}" if query_str else url,
                data=body_to_send,
                headers=headers,
                timeout=self._config.request_timeout,
            )

        self._raise_for_json(r)
        return r.json()

    def _raise_for_json(self, r: requests.Response) -> None:
        try:
            r.raise_for_status()
        except requests.HTTPError as exc:
            try:
                err = r.json()
            except ValueError:
                err = {"raw": r.text}
            raise DexAPIError(f"Aster HTTP {r.status_code}: {err}") from exc

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
