"""Market data utilities for the delta-neutral perp strategy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Iterable, Optional

import numpy as np


@dataclass(frozen=True)
class MarketData:
    """Container holding aligned market data arrays."""

    timestamp: np.ndarray
    spot: np.ndarray
    perp: np.ndarray
    funding_rate: np.ndarray
    open_interest: np.ndarray
    volume: np.ndarray


@dataclass(frozen=True)
class FeatureFrame:
    """Feature table backed by NumPy arrays."""

    data: Dict[str, np.ndarray]

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(next(iter(self.data.values()))) if self.data else 0

    def row(self, index: int) -> Dict[str, object]:
        """Return the feature dictionary for a given index."""

        row: Dict[str, object] = {}
        for key, value in self.data.items():
            element = value[index]
            if isinstance(element, np.generic):
                row[key] = element.item()
            else:
                row[key] = element
        return row


def _exponential_moving_average(values: np.ndarray, span: int) -> np.ndarray:
    alpha = 2.0 / (span + 1.0)
    ema = np.empty_like(values, dtype=float)
    ema[0] = values[0]
    for idx in range(1, len(values)):
        ema[idx] = alpha * values[idx] + (1.0 - alpha) * ema[idx - 1]
    return ema


def _rolling_mean(values: np.ndarray, window: int) -> np.ndarray:
    result = np.empty_like(values, dtype=float)
    if window <= 1:
        result[:] = values
        return result
    if window > len(values):
        result[:] = values.mean() if values.size else 0.0
        return result
    view = np.lib.stride_tricks.sliding_window_view(values, window)
    means = view.mean(axis=-1)
    result[: window - 1] = means[0]
    result[window - 1 :] = means
    return result


def _rolling_std(values: np.ndarray, window: int) -> np.ndarray:
    result = np.empty_like(values, dtype=float)
    if window <= 1:
        result[:] = 0.0
        return result
    if window > len(values):
        result[:] = 0.0
        return result
    view = np.lib.stride_tricks.sliding_window_view(values, window)
    stds = view.std(axis=-1, ddof=0)
    result[: window - 1] = stds[0]
    result[window - 1 :] = stds
    return result


def _diff(values: np.ndarray) -> np.ndarray:
    diffed = np.empty_like(values, dtype=float)
    diffed[0] = 0.0
    diffed[1:] = np.diff(values)
    return diffed


def simulate_feed(
    periods: int,
    seed: Optional[int] = None,
    initial_price: float = 2_000.0,
    drift: float = 0.0,
    volatility: float = 0.02,
    funding_mean: float = 0.0001,
    funding_vol: float = 0.0005,
) -> MarketData:
    """Create a synthetic market data feed using only NumPy primitives."""

    rng = np.random.default_rng(seed)
    returns = drift - 0.5 * volatility**2 + volatility * rng.standard_normal(periods)
    spot = initial_price * np.exp(np.cumsum(returns))

    basis_shock = 0.0005 * rng.standard_normal(periods)
    perp = spot * (1.0 + 0.002 + np.cumsum(basis_shock))

    funding_noise = funding_vol * rng.standard_normal(periods)
    funding_rate = funding_mean + _exponential_moving_average(funding_noise, span=12)

    open_interest = 10_000 + 500 * rng.standard_normal(periods)
    volume = 2_000 + 300 * rng.standard_normal(periods)

    timestamps = np.array([np.datetime64("2023-01-01T00", "h") + np.timedelta64(i, "h") for i in range(periods)])

    return MarketData(
        timestamp=timestamps,
        spot=spot,
        perp=perp,
        funding_rate=funding_rate,
        open_interest=open_interest,
        volume=volume,
    )


def compute_features(frames: Iterable[MarketData]) -> FeatureFrame:
    """Merge raw frames and engineer features for the strategy."""

    arrays = {}
    for frame in frames:
        if not arrays:
            arrays = {field: getattr(frame, field) for field in frame.__dataclass_fields__}
        else:
            for field in frame.__dataclass_fields__:
                arrays[field] = np.concatenate((arrays[field], getattr(frame, field)))

    order = np.argsort(arrays["timestamp"])
    for key in arrays:
        arrays[key] = arrays[key][order]

    spot = arrays["spot"].astype(float)
    perp = arrays["perp"].astype(float)
    funding = arrays["funding_rate"].astype(float)
    volume = arrays["volume"].astype(float)

    basis = (perp - spot) / spot
    basis_spread = _diff(basis)

    spot_return = _diff(np.log(spot))
    perp_return = _diff(np.log(perp))

    realized_vol = _rolling_std(perp_return, window=24) * np.sqrt(24)
    funding_ema = _exponential_moving_average(funding, span=12)

    basis_mean = _rolling_mean(basis, window=48)
    basis_std = _rolling_std(basis, window=48) + 1e-6
    basis_z = (basis - basis_mean) / basis_std

    funding_mean = _rolling_mean(funding, window=48)
    funding_std = _rolling_std(funding, window=48) + 1e-6
    funding_z = (funding - funding_mean) / funding_std

    volume_mean = _rolling_mean(volume, window=12)
    volume_std = _rolling_std(volume, window=12)
    liquidity = np.where(volume_std > 0, volume_mean / volume_std, 0.0)

    vol_of_vol = _rolling_std(realized_vol, window=24)

    data = {
        "timestamp": arrays["timestamp"],
        "spot": spot,
        "perp": perp,
        "funding_rate": funding,
        "open_interest": arrays["open_interest"].astype(float),
        "volume": volume,
        "basis": basis,
        "basis_spread": basis_spread,
        "spot_return": spot_return,
        "perp_return": perp_return,
        "realized_vol": realized_vol,
        "funding_ema": funding_ema,
        "basis_z": basis_z,
        "funding_z": funding_z,
        "liquidity": liquidity,
        "vol_of_vol": vol_of_vol,
    }

    return FeatureFrame(data=data)


__all__ = ["FeatureFrame", "MarketData", "compute_features", "simulate_feed"]
