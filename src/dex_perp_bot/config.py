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
    wallet_address: str


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
class StrategyConfig:
    """Strategy parameters for the delta-neutral bot."""

    leverage: int
    capital_allocation_pct: float
    min_apy_diff_pct: float
    spread_ticks: int
    rebalance_hysteresis_pct: float  # Only rebalance if new opp is this much better (APY %)
    estimated_round_trip_cost_bps: float  # Estimated round-trip cost in basis points (all 4 trades)


@dataclass(frozen=True)
class Settings:
    """Aggregate project configuration loaded from environment variables."""

    hyperliquid: HyperliquidCredentials
    aster: AsterCredentials
    aster_config: AsterConfig
    strategy: StrategyConfig
    discord_webhook_url: Optional[str]

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
            wallet_address=_require_env("HYPERLIQUID_ADDRESS_WALLET"),
        )

        aster_credentials = AsterCredentials(
            api_key=_require_env("ASTER_API_KEY").strip(),
            api_secret=_require_env("ASTER_API_SECRET").strip(),
        )

        aster_config = AsterConfig(
            account_id=None,  # not needed for Aster fapi endpoints
            base_url=os.getenv("ASTER_BASE_URL", "https://fapi.asterdex.com"),
            # prefer /fapi/v4/account because it exposes totals + available in one payload
            balance_endpoint=os.getenv("ASTER_BALANCE_ENDPOINT", "/fapi/v4/account"),
            # v4/account returns a top-level object; leave path empty to use the root
            response_path=_split_path(os.getenv("ASTER_RESPONSE_PATH", "")),
            # "available" candidates: availableBalance, maxWithdrawAmount, totalMarginBalance (fallback)
            available_fields=_split_csv(os.getenv(
                "ASTER_AVAILABLE_FIELDS",
                "availableBalance,maxWithdrawAmount,totalMarginBalance",
            )),
            # "total" candidates from v4/account: totalWalletBalance, totalMarginBalance
            total_fields=_split_csv(os.getenv(
                "ASTER_TOTAL_FIELDS",
                "totalMarginBalance,totalWalletBalance",
            )),
            request_timeout=float(os.getenv("ASTER_TIMEOUT", "10")),
        )

        strategy_config = StrategyConfig(
            leverage=int(os.getenv("STRATEGY_LEVERAGE", "4")),
            capital_allocation_pct=float(os.getenv("STRATEGY_CAPITAL_ALLOCATION_PCT", "0.9")),
            min_apy_diff_pct=float(os.getenv("STRATEGY_MIN_APY_DIFF_PCT", "50")),
            spread_ticks=int(os.getenv("STRATEGY_SPREAD_TICKS", "1")),
            rebalance_hysteresis_pct=float(os.getenv("STRATEGY_REBALANCE_HYSTERESIS_PCT", "20")),
            estimated_round_trip_cost_bps=float(os.getenv("STRATEGY_ROUND_TRIP_COST_BPS", "25")),
        )

        discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None

        return cls(
            hyperliquid=hyperliquid_credentials,
            aster=aster_credentials,
            aster_config=aster_config,
            strategy=strategy_config,
            discord_webhook_url=discord_url,
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

