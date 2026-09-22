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
class ExecutionConfig:
    """Execution / spread-capture parameters (see microstructure.py)."""

    # fees (basis points of notional, per fill)
    hl_maker_bps: float
    hl_taker_bps: float
    aster_maker_bps: float
    aster_taker_bps: float
    # fee break-even gate
    max_breakeven_hours: float      # skip entry if funding needs longer than this to repay costs
    expected_hold_hours: float      # horizon used to value an APY improvement when switching
    # basis z-score
    basis_window_hours: float
    basis_min_samples: int
    z_enter: float                  # favorable z above this = spread cheap for us (informational + logged)
    z_exit: float                   # favorable z below -z_exit and gain >= exit cost -> close to capture basis
    basis_exit_min_gain_bps: float  # extra margin over exit fees before a basis exit is taken
    # order book / queue
    imbalance_threshold: float
    passive_max_wait_s: float       # give a passive leg this long before crossing
    hedge_max_wait_s: float         # once the other leg is filled, cross this fast
    cross_cap_bps: float            # slippage cap for IOC crossing orders
    max_cross_half_spread_bps: float  # never cross a book whose half-spread is wider than this (except to hedge)
    anchor_max_wait_s: float        # patience for the passive leg on the wide book before giving up the entry
    anchor_start_offset_bps: float  # anchor starts this far beyond the touch (better price for us) ...
    anchor_steps: int               # ... and tightens to the touch in this many equal time steps
    repost_min_interval_s: float    # do not chase the touch more often than this
    poll_interval_s: float
    sample_interval_s: float        # basis sampler cadence while idle

    @property
    def round_trip_cost_bps(self) -> float:
        """Open passive on both venues, close taker on both venues (conservative)."""
        return self.hl_maker_bps + self.aster_maker_bps + self.hl_taker_bps + self.aster_taker_bps

    @property
    def exit_cost_bps(self) -> float:
        return self.hl_taker_bps + self.aster_taker_bps

    def fee_bps(self, venue_name: str, maker: bool) -> float:
        if venue_name == "Hyperliquid":
            return self.hl_maker_bps if maker else self.hl_taker_bps
        return self.aster_maker_bps if maker else self.aster_taker_bps


@dataclass(frozen=True)
class Settings:
    """Aggregate project configuration loaded from environment variables."""

    hyperliquid: HyperliquidCredentials
    aster: AsterCredentials
    aster_config: AsterConfig
    strategy: StrategyConfig
    execution: ExecutionConfig
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

        execution_config = ExecutionConfig(
            hl_maker_bps=float(os.getenv("FEE_HL_MAKER_BPS", "1.5")),
            hl_taker_bps=float(os.getenv("FEE_HL_TAKER_BPS", "4.5")),
            aster_maker_bps=float(os.getenv("FEE_ASTER_MAKER_BPS", "1.0")),
            aster_taker_bps=float(os.getenv("FEE_ASTER_TAKER_BPS", "4.0")),
            max_breakeven_hours=float(os.getenv("EXEC_MAX_BREAKEVEN_HOURS", "8")),
            expected_hold_hours=float(os.getenv("EXEC_EXPECTED_HOLD_HOURS", "8")),
            basis_window_hours=float(os.getenv("EXEC_BASIS_WINDOW_HOURS", "6")),
            basis_min_samples=int(os.getenv("EXEC_BASIS_MIN_SAMPLES", "60")),
            z_enter=float(os.getenv("EXEC_Z_ENTER", "1.0")),
            z_exit=float(os.getenv("EXEC_Z_EXIT", "1.5")),
            basis_exit_min_gain_bps=float(os.getenv("EXEC_BASIS_EXIT_MIN_GAIN_BPS", "5")),
            imbalance_threshold=float(os.getenv("EXEC_IMBALANCE_THRESHOLD", "0.3")),
            passive_max_wait_s=float(os.getenv("EXEC_PASSIVE_MAX_WAIT_S", "90")),
            hedge_max_wait_s=float(os.getenv("EXEC_HEDGE_MAX_WAIT_S", "15")),
            cross_cap_bps=float(os.getenv("EXEC_CROSS_CAP_BPS", "20")),
            max_cross_half_spread_bps=float(os.getenv("EXEC_MAX_CROSS_HALF_SPREAD_BPS", "3")),
            anchor_max_wait_s=float(os.getenv("EXEC_ANCHOR_MAX_WAIT_S", "2400")),
            anchor_start_offset_bps=float(os.getenv("EXEC_ANCHOR_START_OFFSET_BPS", "8")),
            anchor_steps=int(os.getenv("EXEC_ANCHOR_STEPS", "4")),
            repost_min_interval_s=float(os.getenv("EXEC_REPOST_MIN_INTERVAL_S", "10")),
            poll_interval_s=float(os.getenv("EXEC_POLL_INTERVAL_S", "3")),
            sample_interval_s=float(os.getenv("EXEC_SAMPLE_INTERVAL_S", "30")),
        )

        discord_url = os.getenv("DISCORD_WEBHOOK_URL", "").strip() or None

        return cls(
            hyperliquid=hyperliquid_credentials,
            aster=aster_credentials,
            aster_config=aster_config,
            strategy=strategy_config,
            execution=execution_config,
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

