"""Pure execution-signal maths: order-book imbalance, queue position, basis z-score, fee break-even.

Everything here is deterministic and side-effect free so it can be unit tested and
replayed from the decision log. Prices/quantities are Decimals; time is seconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from decimal import Decimal, ROUND_CEILING, ROUND_FLOOR
from statistics import mean, pstdev
from typing import Iterable, List, Optional, Sequence, Tuple

Level = Tuple[Decimal, Decimal]  # (price, quantity)
Trade = Tuple[float, Decimal, Decimal, str]  # (ts_seconds, price, qty, aggressor side "buy"/"sell")

BPS = Decimal(10_000)
HOURS_PER_YEAR = Decimal(24 * 365)


# ---------------------------------------------------------------------------
# 1. Order-book imbalance
# ---------------------------------------------------------------------------

def imbalance(bids: Sequence[Level], asks: Sequence[Level], levels: int = 5) -> Decimal:
    """(bid_qty - ask_qty) / (bid_qty + ask_qty) over the top ``levels`` of each side.

    +1 = only bids (buyers dominate, next move likely up), -1 = only asks. 0 when empty.
    """
    bid_qty = sum((q for _, q in bids[:levels]), Decimal(0))
    ask_qty = sum((q for _, q in asks[:levels]), Decimal(0))
    total = bid_qty + ask_qty
    if total == 0:
        return Decimal(0)
    return (bid_qty - ask_qty) / total


# ---------------------------------------------------------------------------
# 2. Queue position
# ---------------------------------------------------------------------------

def queue_ahead(book_side: Sequence[Level], price: Decimal) -> Decimal:
    """Quantity already resting at ``price`` on that side (what fills before a new order there)."""
    for level_price, qty in book_side:
        if level_price == price:
            return qty
    return Decimal(0)


def aggressive_flow_rate(trades: Iterable[Trade], aggressor_side: str, window_s: float, now_s: float) -> Decimal:
    """Quantity per second traded by aggressors on ``aggressor_side`` during the last ``window_s``.

    A resting BUY at the best bid is consumed by aggressive sells, so pass "sell".
    """
    if window_s <= 0:
        return Decimal(0)
    trades = list(trades)
    if not trades:
        return Decimal(0)
    # Venues return a bounded number of trades; if they cover less than the window,
    # measure over the span they actually cover (at least 1 s) instead of diluting the rate.
    span = now_s - min(ts for ts, *_ in trades)
    effective_window = max(min(window_s, span), 1.0)
    cutoff = now_s - effective_window
    total = sum((qty for ts, _, qty, side in trades if side == aggressor_side and ts >= cutoff), Decimal(0))
    return total / Decimal(str(round(effective_window, 3)))


def expected_wait_s(queue_qty: Decimal, flow_rate_per_s: Decimal) -> float:
    """Seconds until ``queue_qty`` ahead of us is consumed at ``flow_rate_per_s``. inf if no flow."""
    if flow_rate_per_s <= 0:
        return math.inf
    return float(queue_qty / flow_rate_per_s)


# ---------------------------------------------------------------------------
# 3. Basis z-score
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class BasisStats:
    n: int
    mean_bps: Decimal
    std_bps: Decimal
    current_bps: Decimal
    z: Optional[Decimal]  # None when not enough samples or zero variance


def basis_bps(price_aster: Decimal, price_hl: Decimal) -> Decimal:
    """Aster premium over Hyperliquid in basis points: (aster - hl) / hl * 1e4."""
    if price_hl == 0:
        return Decimal(0)
    return (price_aster - price_hl) / price_hl * BPS


def basis_stats(history_bps: Sequence[Decimal], current_bps: Decimal, min_samples: int = 30) -> BasisStats:
    n = len(history_bps)
    if n < min_samples:
        return BasisStats(n=n, mean_bps=Decimal(0), std_bps=Decimal(0), current_bps=current_bps, z=None)
    floats = [float(x) for x in history_bps]
    mu = Decimal(str(mean(floats)))
    sigma = Decimal(str(pstdev(floats)))
    z = (current_bps - mu) / sigma if sigma > 0 else None
    return BasisStats(n=n, mean_bps=mu, std_bps=sigma, current_bps=current_bps, z=z)


def favorable_sign(long_venue: str) -> int:
    """+1 when a rising basis (Aster richer) helps the position, -1 otherwise.

    Long Aster / short HL profits when Aster rises relative to HL (basis up).
    """
    return 1 if long_venue == "Aster" else -1


def expected_reversion_bps(stats: BasisStats, long_venue: str) -> Decimal:
    """Basis gain (positive = in our favour) if basis reverts from current to its mean."""
    if stats.z is None:
        return Decimal(0)
    return Decimal(favorable_sign(long_venue)) * (stats.mean_bps - stats.current_bps)


def favorable_z(stats: BasisStats, long_venue: str) -> Optional[Decimal]:
    """z-score signed so that positive = basis currently cheap for this direction."""
    if stats.z is None:
        return None
    return Decimal(-favorable_sign(long_venue)) * stats.z


def basis_gain_bps(entry_bps: Decimal, current_bps: Decimal, long_venue: str) -> Decimal:
    """Realised basis move since entry, positive when it helped the position."""
    return Decimal(favorable_sign(long_venue)) * (current_bps - entry_bps)


# ---------------------------------------------------------------------------
# 4. Funding vs fee break-even
# ---------------------------------------------------------------------------

def funding_bps_per_hour(net_apy_pct: Decimal) -> Decimal:
    """Net APY in percent -> basis points of notional earned per hour."""
    return net_apy_pct / Decimal(100) * BPS / HOURS_PER_YEAR


def breakeven_hours(cost_bps: Decimal, net_apy_pct: Decimal) -> float:
    """Hours of funding needed to repay ``cost_bps`` (fees minus expected basis gain). inf if no income."""
    hourly = funding_bps_per_hour(net_apy_pct)
    if hourly <= 0:
        return math.inf
    if cost_bps <= 0:
        return 0.0
    return float(cost_bps / hourly)


# ---------------------------------------------------------------------------
# Leg tactic
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class LegPlan:
    tactic: str  # "passive" (post-only join best) or "cross" (IOC through the touch)
    reason: str
    imbalance: Decimal
    queue_qty: Decimal
    flow_rate: Decimal
    expected_wait_s: float


def plan_leg(
    side: str,
    bids: Sequence[Level],
    asks: Sequence[Level],
    trades: Sequence[Trade],
    *,
    now_s: float,
    imbalance_threshold: Decimal,
    max_wait_s: float,
    flow_window_s: float = 120.0,
    levels: int = 5,
) -> LegPlan:
    """Decide passive vs cross for one leg from imbalance and queue/flow.

    BUY: strong positive imbalance (buyers pushing) -> price about to move up, cross now.
         Otherwise join the best bid; but if the queue there will take longer than
         ``max_wait_s`` to clear, cross instead.
    SELL: mirrored.
    """
    side = side.lower()
    imb = imbalance(bids, asks, levels)
    if side == "buy":
        best = bids[0][0] if bids else Decimal(0)
        q = queue_ahead(bids, best)
        rate = aggressive_flow_rate(trades, "sell", flow_window_s, now_s)
        adverse = imb >= imbalance_threshold
    else:
        best = asks[0][0] if asks else Decimal(0)
        q = queue_ahead(asks, best)
        rate = aggressive_flow_rate(trades, "buy", flow_window_s, now_s)
        adverse = imb <= -imbalance_threshold
    wait = expected_wait_s(q, rate)
    if adverse:
        return LegPlan("cross", f"imbalance {imb:+.2f} against a passive {side}", imb, q, rate, wait)
    if wait > max_wait_s:
        return LegPlan("cross", f"queue {q} at touch, flow {rate:.4f}/s -> wait {wait:.0f}s > {max_wait_s:.0f}s", imb, q, rate, wait)
    return LegPlan("passive", f"imbalance {imb:+.2f}, expected wait {wait:.0f}s", imb, q, rate, wait)


# ---------------------------------------------------------------------------
# Price helpers
# ---------------------------------------------------------------------------

def round_to_tick(price: Decimal, tick: Decimal, side: str) -> Decimal:
    """Round a BUY price down and a SELL price up to the tick grid (never more aggressive than asked)."""
    if tick <= 0:
        return price
    rounding = ROUND_FLOOR if side.lower() == "buy" else ROUND_CEILING
    return (price / tick).to_integral_value(rounding=rounding) * tick


def cross_price(side: str, best_bid: Decimal, best_ask: Decimal, cap_bps: Decimal, tick: Decimal) -> Decimal:
    """Limit price that crosses the touch with a slippage cap: BUY at ask*(1+cap), SELL at bid*(1-cap)."""
    if side.lower() == "buy":
        raw = best_ask * (1 + cap_bps / BPS)
        return round_to_tick(raw, tick, "sell")  # round up so we still cross
    raw = best_bid * (1 - cap_bps / BPS)
    return round_to_tick(raw, tick, "buy")  # round down


def slippage_bps(side: str, fill_price: Decimal, reference_mid: Decimal) -> Decimal:
    """Cost of the fill versus the reference mid, positive = paid more than mid (worse)."""
    if reference_mid == 0:
        return Decimal(0)
    diff = (fill_price - reference_mid) / reference_mid * BPS
    return diff if side.lower() == "buy" else -diff
