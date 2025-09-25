"""Shared exchange primitives."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable, Mapping, MutableMapping, Optional, Sequence


class DexClientError(Exception):
    """Base exception for exchange clients."""


class DexAPIError(DexClientError):
    """Raised when an API request fails or returns an unexpected response."""


class BalanceParsingError(DexClientError):
    """Raised when the response payload does not contain expected balance fields."""


@dataclass(frozen=True)
class WalletBalance:
    """Structured representation of a wallet balance."""

    total: Optional[Decimal]
    available: Optional[Decimal]
    raw: Mapping[str, Any]

    def as_dict(self) -> MutableMapping[str, Optional[str]]:
        """Return a JSON-serialisable representation of the balance."""

        return {
            "total": _decimal_to_str(self.total),
            "available": _decimal_to_str(self.available),
            "raw": self.raw,
        }


def _decimal_to_str(value: Optional[Decimal]) -> Optional[str]:
    return format(value, "f") if isinstance(value, Decimal) else None


def to_decimal(value: Any) -> Optional[Decimal]:
    """Convert the provided value into :class:`~decimal.Decimal` if possible."""

    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    try:
        return Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError) as exc:  # pragma: no cover - defensive
        raise BalanceParsingError(f"Unable to parse decimal from value: {value!r}") from exc


def get_from_path(payload: Mapping[str, Any], path: Sequence[str]) -> Any:
    """Safely traverse a nested mapping and return the value for the given path."""

    current: Any = payload
    for segment in path:
        if not isinstance(current, Mapping) or segment not in current:
            return None
        current = current[segment]
    return current


def find_first_key(payload: Mapping[str, Any], keys: Iterable[str]) -> Any:
    """Return the first value present in ``payload`` keyed by any item in ``keys``."""

    for key in keys:
        if isinstance(payload, Mapping) and key in payload:
            return payload[key]
    return None

