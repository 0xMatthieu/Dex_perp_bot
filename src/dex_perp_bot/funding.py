"""Utilities for querying perp funding rates.

This module provides helpers to fetch the most recent funding rate from
Hyperliquid and Aster.  Both exchanges expose HTTP APIs that we query
through :mod:`requests`.  The helpers are intentionally defensive so that
minor schema changes from the exchanges do not immediately break the bot.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional

import requests

__all__ = [
    "FundingRate",
    "FundingQueryError",
    "fetch_hyperliquid_funding",
    "fetch_aster_funding",
    "query_funding_once",
    "run_funding_poll",
]

logger = logging.getLogger(__name__)

_HYPERLIQUID_INFO_URL = "https://api.hyperliquid.xyz/info"
# Public REST endpoint documented at https://docs.aster.trade/reference/public_data
# which supports `symbol` query parameter and returns the latest funding entry.
_ASTER_FUNDING_URL = "https://api.prod.asterperp.xyz/api/v1/public/funding"
_REQUEST_TIMEOUT = 10
_DEFAULT_LOOKBACK_SECONDS = 60 * 60 * 8  # 8 hours is plenty to get the latest entry.


class FundingQueryError(RuntimeError):
    """Raised when an exchange funding query cannot be fulfilled."""


@dataclass
class FundingRate:
    """Container that represents a single funding observation."""

    exchange: str
    market: str
    rate: float
    timestamp: Optional[datetime]
    raw: Dict[str, Any]

    def __repr__(self) -> str:  # pragma: no cover - representation helper only
        timestamp = self.timestamp.isoformat() if self.timestamp else "?"
        return (
            f"FundingRate(exchange={self.exchange!r}, market={self.market!r},"
            f" rate={self.rate:.6f}, timestamp={timestamp})"
        )


def _ensure_session(session: Optional[requests.Session]) -> requests.Session:
    return session if session is not None else requests.Session()


def _maybe_to_float(value: Any) -> float:
    if value is None:
        raise FundingQueryError("Funding response did not contain a rate value")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:  # pragma: no cover - defensive branch
        raise FundingQueryError(f"Unable to convert funding rate {value!r} to float") from exc


def _extract_timestamp(entry: Dict[str, Any]) -> Optional[datetime]:
    timestamp_fields = ("time", "timestamp", "ts", "createdAt", "created_at", "updatedAt")
    for field in timestamp_fields:
        if field in entry:
            value = entry[field]
            try:
                value = float(value)
            except (TypeError, ValueError):  # pragma: no cover - defensive branch
                logger.debug("Ignoring non-numeric timestamp %s=%r", field, value)
                continue
            if value > 1e12:
                value /= 1000.0
            return datetime.fromtimestamp(value, tz=timezone.utc)
    return None


def _latest_entry(entries: Any) -> Dict[str, Any]:
    if isinstance(entries, dict):
        # Many APIs return a single funding entry in a dictionary already.
        return entries

    if not isinstance(entries, list) or not entries:
        raise FundingQueryError("Funding response did not include any entries")

    return max(entries, key=lambda entry: entry.get("time") or entry.get("timestamp") or 0)


def fetch_hyperliquid_funding(
    market: str,
    *,
    session: Optional[requests.Session] = None,
    lookback_seconds: int = _DEFAULT_LOOKBACK_SECONDS,
) -> FundingRate:
    """Fetch the latest funding rate for a Hyperliquid market.

    Args:
        market: The Hyperliquid market symbol (for example ``"ETH-PERP"``).
        session: Optional :class:`requests.Session` to reuse connections.
        lookback_seconds: Range to pull historical data for.  The latest
            funding entry within this window is returned.

    Returns:
        :class:`FundingRate` describing the most recent funding observation.

    Raises:
        FundingQueryError: If the request fails or the response cannot be parsed.
    """

    close_session = session is None
    session = _ensure_session(session)

    payload = {
        "type": "fundingHistory",
        "coin": market,
        "startTime": int((time.time() - lookback_seconds) * 1000),
    }

    logger.debug("Requesting Hyperliquid funding: %s", payload)

    try:
        response = session.post(
            _HYPERLIQUID_INFO_URL,
            json=payload,
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:  # pragma: no cover - network failure
        raise FundingQueryError("Hyperliquid funding request failed") from exc
    finally:
        if close_session:
            session.close()

    data = response.json()
    if isinstance(data, dict) and "data" in data:
        data = data["data"]
    latest = _latest_entry(data)

    rate = (
        latest.get("fundingRate")
        or latest.get("funding_rate")
        or latest.get("rate")
    )
    funding_rate = _maybe_to_float(rate)

    return FundingRate(
        exchange="hyperliquid",
        market=market,
        rate=funding_rate,
        timestamp=_extract_timestamp(latest),
        raw=latest,
    )


def fetch_aster_funding(
    market: str,
    *,
    session: Optional[requests.Session] = None,
) -> FundingRate:
    """Fetch the latest funding rate for an Aster market.

    Args:
        market: The Aster market symbol (for example ``"ETH-PERP"``).
        session: Optional :class:`requests.Session` to reuse connections.

    Returns:
        :class:`FundingRate` describing the most recent funding observation.

    Raises:
        FundingQueryError: If the request fails or the response cannot be parsed.
    """

    close_session = session is None
    session = _ensure_session(session)

    logger.debug("Requesting Aster funding for %s", market)

    try:
        response = session.get(
            _ASTER_FUNDING_URL,
            params={"symbol": market},
            timeout=_REQUEST_TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as exc:  # pragma: no cover - network failure
        raise FundingQueryError("Aster funding request failed") from exc
    finally:
        if close_session:
            session.close()

    payload = response.json()
    data = payload.get("data", payload)
    if isinstance(data, dict) and "entries" in data:
        latest = _latest_entry(data["entries"])
    else:
        latest = _latest_entry(data)

    rate = (
        latest.get("fundingRate")
        or latest.get("funding_rate")
        or latest.get("rate")
    )
    funding_rate = _maybe_to_float(rate)

    return FundingRate(
        exchange="aster",
        market=market,
        rate=funding_rate,
        timestamp=_extract_timestamp(latest),
        raw=latest,
    )


def query_funding_once(
    *,
    hyperliquid_market: str,
    aster_market: str,
    session: Optional[requests.Session] = None,
) -> Dict[str, FundingRate]:
    """Query both exchanges once and return their latest funding observations.

    The helper is convenient for polling code because it reuses the same
    :class:`requests.Session` for both exchanges and returns a dictionary that
    is easy to log or inspect.
    """

    close_session = session is None
    session = _ensure_session(session)

    try:
        hyperliquid = fetch_hyperliquid_funding(
            hyperliquid_market, session=session
        )
        aster = fetch_aster_funding(aster_market, session=session)
    finally:
        if close_session:
            session.close()

    return {
        "hyperliquid": hyperliquid,
        "aster": aster,
    }


def run_funding_poll(
    *,
    hyperliquid_market: str,
    aster_market: str,
    interval: float = 30.0,
    callback: Optional[Any] = None,
    session: Optional[requests.Session] = None,
) -> None:
    """Continuously poll funding data at the provided cadence.

    Args:
        hyperliquid_market: Market symbol to query on Hyperliquid.
        aster_market: Market symbol to query on Aster.
        interval: Number of seconds to wait between queries.
        callback: Optional callable invoked with the funding dictionary after
            each poll.  When omitted, the results are logged via :mod:`logging`.
        session: Optional :class:`requests.Session` to reuse connections.

    Raises:
        ValueError: If ``interval`` is not positive.
    """

    if interval <= 0:
        raise ValueError("interval must be positive")

    while True:
        rates = query_funding_once(
            hyperliquid_market=hyperliquid_market,
            aster_market=aster_market,
            session=session,
        )
        if callback is None:
            logger.info("Funding update: %s", rates)
        else:
            callback(rates)
        time.sleep(interval)
