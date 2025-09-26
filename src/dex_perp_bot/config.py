"""Configuration helpers for the Dex Perp Bot project."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Tuple
import os

from dotenv import load_dotenv


@dataclass(frozen=True)
class HyperliquidCredentials:
    """Hyperliquid API authentication bundle."""

    private_key: str


@dataclass(frozen=True)
class AsterCredentials:
    """Aster API authentication bundle."""

    api_key: str
    api_secret: str


@dataclass(frozen=True)
class AsterConfig:
    """Aster API configuration and balance parsing metadata."""

    account_id: Optional[str]
    base_url: str
    balance_endpoint: str
    response_path: Tuple[str, ...]
    available_fields: Tuple[str, ...]
    total_fields: Tuple[str, ...]
    request_timeout: float = 10.0


@dataclass(frozen=True)
class Settings:
    """Aggregate project configuration loaded from environment variables."""

    hyperliquid: HyperliquidCredentials
    aster: AsterCredentials
    aster_config: AsterConfig

    @classmethod
    def from_env(cls, *, load_env_file: bool = True) -> "Settings":
        """Instantiate settings from environment variables.

        Args:
            load_env_file: If ``True`` (default) a `.env` file located in the
                project root will be loaded before accessing the environment.

        Raises:
            ValueError: If any required configuration item is missing.
        """

        if load_env_file:
            load_dotenv()

        hyperliquid_credentials = HyperliquidCredentials(
            private_key=_require_env("HYPERLIQUID_PRIVATE_KEY"),
        )

        aster_credentials = AsterCredentials(
            api_key=_require_env("ASTER_API_KEY"),
            api_secret=_require_env("ASTER_API_SECRET"),
        )
        aster_config = AsterConfig(
            account_id=os.getenv("ASTER_ACCOUNT_ID"),
            base_url=os.getenv("ASTER_BASE_URL", "https://api.prod.asterperp.xyz/api"),
            balance_endpoint=os.getenv("ASTER_BALANCE_ENDPOINT", "/v1/perp/account-summary"),
            response_path=_split_path(os.getenv("ASTER_RESPONSE_PATH", "data.account")),
            available_fields=_split_csv(
                os.getenv(
                    "ASTER_AVAILABLE_FIELDS",
                    "availableBalance,availableMargin,freeCollateral,withdrawable",
                )
            ),
            total_fields=_split_csv(
                os.getenv(
                    "ASTER_TOTAL_FIELDS",
                    "totalBalance,totalCollateral,accountValue",
                )
            ),
            request_timeout=float(os.getenv("ASTER_TIMEOUT", "10")),
        )

        return cls(
            hyperliquid=hyperliquid_credentials,
            aster=aster_credentials,
            aster_config=aster_config,
        )


def _require_env(name: str) -> str:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        raise ValueError(f"Missing required environment variable: {name}")
    return value


def _split_path(path: str) -> Tuple[str, ...]:
    parts = [segment.strip() for segment in path.split(".") if segment.strip()]
    return tuple(parts)


def _split_csv(raw: str) -> Tuple[str, ...]:
    return tuple(segment.strip() for segment in raw.split(",") if segment.strip())

