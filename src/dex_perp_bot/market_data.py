"""Market data utilities for the delta-neutral perp strategy."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, Optional

import numpy as np
import pandas as pd


@dataclass(frozen=True)
class MarketSnapshot:
    """Represents a snapshot of market data for spot and perpetual contracts."""

    timestamp: pd.Timestamp
    spot: float
    perp: float
    funding_rate: float
    open_interest: float
    volume: float

    def basis(self) -> float:
        """Return the relative basis between the perp and spot price."""
        return (self.perp - self.spot) / self.spot


def simulate_feed(
    periods: int,
    seed: Optional[int] = None,
    initial_price: float = 2_000.0,
    drift: float = 0.0,
    volatility: float = 0.02,
    funding_mean: float = 0.0001,
    funding_vol: float = 0.0005,
) -> pd.DataFrame:
    """Create a synthetic market data feed.

    The generator creates prices via geometric Brownian motion with a small drift
    and adds mildly autocorrelated funding rates and open interest. Although
    synthetic, this feed is useful for demonstrating the strategy logic without
    relying on an exchange connection.
    """
    rng = np.random.default_rng(seed)
    returns = drift - 0.5 * volatility**2 + volatility * rng.standard_normal(periods)
    spot = initial_price * np.exp(np.cumsum(returns))

    basis_shock = 0.0005 * rng.standard_normal(periods)
    perp = spot * (1.0 + 0.002 + np.cumsum(basis_shock))

    funding_noise = funding_vol * rng.standard_normal(periods)
    funding_rate = funding_mean + pd.Series(funding_noise).ewm(alpha=0.2).mean().to_numpy()

    open_interest = 10_000 + 500 * rng.standard_normal(periods)
    volume = 2_000 + 300 * rng.standard_normal(periods)

    index = pd.date_range("2023-01-01", periods=periods, freq="h", tz="UTC")

    data = pd.DataFrame(
        {
            "timestamp": index,
            "spot": spot,
            "perp": perp,
            "funding_rate": funding_rate,
            "open_interest": open_interest,
            "volume": volume,
        }
    )
    data["basis"] = (data["perp"] - data["spot"]) / data["spot"]
    data["basis_spread"] = data["basis"].diff().fillna(0.0)
    return data


def compute_features(frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    """Merge raw frames and engineer features for the strategy.

    Parameters
    ----------
    frames:
        Iterable of frames containing market data. They are concatenated in
        chronological order.
    """
    df = pd.concat(list(frames), ignore_index=True)
    df = df.sort_values("timestamp")
    df = df.set_index("timestamp")

    df["spot_return"] = np.log(df["spot"]).diff().fillna(0.0)
    df["perp_return"] = np.log(df["perp"]).diff().fillna(0.0)

    df["realized_vol"] = df["perp_return"].rolling(window=24).std().bfill() * np.sqrt(24)
    df["funding_ema"] = df["funding_rate"].ewm(span=12, adjust=False).mean()
    df["basis_z"] = (
        (df["basis"] - df["basis"].rolling(window=48).mean())
        / (df["basis"].rolling(window=48).std() + 1e-6)
    ).fillna(0.0)
    df["funding_z"] = (
        (df["funding_rate"] - df["funding_rate"].rolling(window=48).mean())
        / (df["funding_rate"].rolling(window=48).std() + 1e-6)
    ).fillna(0.0)

    df["liquidity"] = (df["volume"].rolling(window=12).mean() / df["volume"].rolling(window=12).std()).fillna(0.0)
    df["vol_of_vol"] = df["realized_vol"].rolling(window=24).std().bfill()

    return df
