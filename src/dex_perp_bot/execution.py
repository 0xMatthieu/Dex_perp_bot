"""Two-leg execution with per-leg tactics and hedge-first repricing.

For each leg the plan is chosen from order-book imbalance and queue/flow
(``microstructure.plan_leg``). Passive legs are post-only at the touch; if they
have not filled by their deadline (or the other leg is already filled and we are
naked), the remainder is crossed with an IOC limit under a slippage cap.

Every step is written to the decision log so fills can be audited later.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Dict, List, Optional

from .config import ExecutionConfig
from .decision_log import log_event
from .exchanges.base import DexAPIError
from .microstructure import LegPlan, cross_price, plan_leg, round_to_tick, slippage_bps

logger = logging.getLogger(__name__)


@dataclass
class Leg:
    venue: Any  # AsterClient | HyperliquidClient (both expose the execution primitives)
    symbol: str
    side: str  # "buy" | "sell"
    quantity: Decimal
    reduce_only: bool = False
    # filled in during execution
    plan: Optional[LegPlan] = None
    reference_mid: Decimal = Decimal(0)
    tick: Decimal = Decimal(0)
    step: Decimal = Decimal(0)
    order_id: Optional[str] = None
    current_tactic: str = ""
    filled: Decimal = Decimal(0)
    fill_notional: Decimal = Decimal(0)
    placed_at: float = 0.0
    deadline: float = math.inf
    done: bool = False
    crossed_after_wait: bool = False
    cross_attempts: int = 0
    error: Optional[str] = None
    fills_log: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def venue_name(self) -> str:
        return getattr(self.venue, "venue_name", type(self.venue).__name__)

    @property
    def remaining(self) -> Decimal:
        return max(self.quantity - self.filled, Decimal(0))

    @property
    def avg_price(self) -> Optional[Decimal]:
        return self.fill_notional / self.filled if self.filled > 0 else None


@dataclass
class PairResult:
    legs: List[Leg]
    hedged: bool
    elapsed_s: float

    def summary(self) -> Dict[str, Any]:
        return {
            "hedged": self.hedged,
            "elapsed_s": round(self.elapsed_s, 1),
            "legs": [
                {
                    "venue": l.venue_name, "symbol": l.symbol, "side": l.side, "qty": l.quantity,
                    "filled": l.filled, "avg_price": l.avg_price, "tactic": l.current_tactic,
                    "planned": l.plan.tactic if l.plan else None, "error": l.error,
                }
                for l in self.legs
            ],
        }


def _mid(book: Dict[str, List]) -> Decimal:
    bids, asks = book.get("bids") or [], book.get("asks") or []
    if bids and asks:
        return (bids[0][0] + asks[0][0]) / 2
    if bids:
        return bids[0][0]
    if asks:
        return asks[0][0]
    return Decimal(0)


def _round_qty(qty: Decimal, step: Decimal) -> Decimal:
    if step <= 0:
        return qty
    return (qty // step) * step


def _submit(leg: Leg, tactic: str, cfg: ExecutionConfig, context: str) -> None:
    """Place (or re-place) the remaining quantity of a leg with the given tactic."""
    book = leg.venue.get_book(leg.symbol, depth=5)
    bids, asks = book["bids"], book["asks"]
    if not bids or not asks:
        raise DexAPIError(f"Empty book for {leg.symbol} on {leg.venue_name}")
    qty = _round_qty(leg.remaining, leg.step)
    if qty <= 0:
        leg.done = True
        return
    if tactic == "passive":
        price = bids[0][0] if leg.side == "buy" else asks[0][0]
        price = round_to_tick(price, leg.tick, leg.side)
        order_id = leg.venue.place_limit(leg.symbol, leg.side, qty, price, post_only=True, reduce_only=leg.reduce_only)
        leg.deadline = time.time() + cfg.passive_max_wait_s
    else:
        price = cross_price(leg.side, bids[0][0], asks[0][0], Decimal(str(cfg.cross_cap_bps)), leg.tick)
        order_id = leg.venue.place_limit(leg.symbol, leg.side, qty, price, ioc=True, reduce_only=leg.reduce_only)
        leg.cross_attempts += 1
        leg.deadline = time.time() + max(cfg.poll_interval_s * 2, 5.0)
    leg.order_id = order_id
    leg.current_tactic = tactic
    leg.placed_at = leg.placed_at or time.time()
    logger.info("[%s] %s %s %s @ %s (%s, %s) order %s", leg.venue_name, leg.side.upper(), qty, leg.symbol, price, tactic, context, order_id)
    log_event("leg_order", venue=leg.venue_name, symbol=leg.symbol, side=leg.side, qty=qty, price=price,
              tactic=tactic, context=context, order_id=order_id, best_bid=bids[0][0], best_ask=asks[0][0])


def _refresh(leg: Leg) -> str:
    """Poll the venue and fold new fills into the leg. Returns status open|filled|canceled."""
    state = leg.venue.get_order_state(leg.symbol, leg.order_id)
    filled_now = state.get("filled") or Decimal(0)
    avg = state.get("avg_price")
    # Fold in incremental fills for this order id (orders are replaced, so track per order).
    prev = sum((f["filled"] for f in leg.fills_log if f["order_id"] == leg.order_id), Decimal(0))
    delta = filled_now - prev
    if delta > 0 and not avg:  # last resort so a fill is never dropped from the accounting
        avg = leg.reference_mid
        logger.warning("[%s] order %s filled %s without an average price; using reference mid", leg.venue_name, leg.order_id, delta)
    if delta > 0 and avg:
        leg.filled += delta
        leg.fill_notional += delta * avg
        leg.fills_log.append({"order_id": leg.order_id, "filled": filled_now, "avg_price": avg, "tactic": leg.current_tactic})
    status = state.get("status", "open")
    if leg.remaining <= 0 or (status == "filled"):
        leg.done = True
        status = "filled"
    return status


def _cancel(leg: Leg) -> None:
    if leg.order_id is None:
        return
    try:
        leg.venue.cancel_by_id(leg.symbol, leg.order_id)
    except Exception as exc:  # cancel failures are logged; the poll loop re-checks state
        logger.warning("[%s] cancel %s failed: %s", leg.venue_name, leg.order_id, exc)
    try:
        _refresh(leg)  # capture any fill that landed before the cancel
    except Exception as exc:
        logger.warning("[%s] post-cancel refresh failed: %s", leg.venue_name, exc)


def _finish_leg(leg: Leg, context: str) -> None:
    wait_s = (time.time() - leg.placed_at) if leg.placed_at else None
    slip = slippage_bps(leg.side, leg.avg_price, leg.reference_mid) if leg.avg_price else None
    log_event(
        "leg_fill", venue=leg.venue_name, symbol=leg.symbol, side=leg.side, qty=leg.quantity, filled=leg.filled,
        avg_price=leg.avg_price, reference_mid=leg.reference_mid, slippage_bps=slip, wait_s=wait_s,
        planned_tactic=leg.plan.tactic if leg.plan else None, final_tactic=leg.current_tactic,
        crossed_after_wait=leg.crossed_after_wait, cross_attempts=leg.cross_attempts,
        imbalance=leg.plan.imbalance if leg.plan else None, queue_qty=leg.plan.queue_qty if leg.plan else None,
        expected_wait_s=leg.plan.expected_wait_s if leg.plan else None, error=leg.error, context=context,
    )


def execute_pair(legs: List[Leg], cfg: ExecutionConfig, *, context: str = "entry", max_total_s: float = 300.0) -> PairResult:
    """Run both legs to completion (or timeout). Never raises; inspect ``PairResult``."""
    start = time.time()
    now = start

    # 1. Plan each leg from its own book and trade flow.
    for leg in legs:
        try:
            leg.tick, leg.step = leg.venue.get_increments(leg.symbol)
            book = leg.venue.get_book(leg.symbol, depth=5)
            trades = leg.venue.get_recent_trades(leg.symbol, limit=100)
            leg.reference_mid = _mid(book)
            leg.plan = plan_leg(
                leg.side, book["bids"], book["asks"], trades, now_s=now,
                imbalance_threshold=Decimal(str(cfg.imbalance_threshold)), max_wait_s=cfg.passive_max_wait_s,
            )
            log_event("leg_plan", venue=leg.venue_name, symbol=leg.symbol, side=leg.side, qty=leg.quantity,
                      tactic=leg.plan.tactic, reason=leg.plan.reason, imbalance=leg.plan.imbalance,
                      queue_qty=leg.plan.queue_qty, flow_rate=leg.plan.flow_rate,
                      expected_wait_s=leg.plan.expected_wait_s, mid=leg.reference_mid, context=context)
        except Exception as exc:
            leg.error = f"plan: {exc}"
            leg.plan = LegPlan("cross", f"planning failed ({exc}); crossing", Decimal(0), Decimal(0), Decimal(0), 0.0)
            logger.warning("[%s] planning %s failed: %s -> cross", leg.venue_name, leg.symbol, exc)

    # 2. Submit.
    for leg in legs:
        try:
            _submit(leg, leg.plan.tactic if leg.plan else "cross", cfg, context)
        except Exception as exc:
            leg.error = f"submit: {exc}"
            logger.error("[%s] submit %s failed: %s", leg.venue_name, leg.symbol, exc)

    # 3. Manage until both done or overall timeout.
    while time.time() - start < max_total_s:
        open_legs = [l for l in legs if not l.done and l.order_id]
        if not open_legs and all(l.done or l.error for l in legs):
            break
        any_filled = any(l.filled > 0 for l in legs)
        for leg in open_legs:
            try:
                status = _refresh(leg)
            except Exception as exc:
                logger.warning("[%s] poll %s failed: %s", leg.venue_name, leg.order_id, exc)
                continue
            if leg.done:
                continue
            now = time.time()
            # Hedge urgency: the other leg has (partially) filled and we are still resting.
            other_filled = any(l is not leg and l.filled > 0 for l in legs)
            if other_filled and leg.current_tactic == "passive":
                leg.deadline = min(leg.deadline, leg.placed_at + cfg.hedge_max_wait_s)
            if status == "canceled" or now >= leg.deadline:
                # Passive expired (or IOC left a remainder): cross the rest.
                if leg.current_tactic == "passive":
                    _cancel(leg)
                    if leg.done:
                        continue
                    leg.crossed_after_wait = True
                if leg.cross_attempts >= 4:
                    leg.error = "cross attempts exhausted"
                    leg.done = True
                    continue
                try:
                    _submit(leg, "cross", cfg, f"{context}:reprice" + (":hedge" if other_filled else ""))
                except Exception as exc:
                    leg.error = f"cross: {exc}"
                    leg.done = True
        # Retry legs whose initial submit failed while the other side has exposure.
        for leg in legs:
            if leg.error and leg.error.startswith("submit:") and not leg.order_id and any_filled and leg.cross_attempts < 2:
                try:
                    _submit(leg, "cross", cfg, f"{context}:retry")
                    leg.error = None
                except Exception as exc:
                    leg.error = f"submit: {exc}"
        time.sleep(cfg.poll_interval_s)

    # 4. Final cleanup: cancel anything still resting, record outcomes.
    for leg in legs:
        if not leg.done and leg.order_id:
            _cancel(leg)
            leg.done = True
            if leg.remaining > 0 and not leg.error:
                leg.error = "timeout"
        _finish_leg(leg, context)

    filled_notionals = [l.fill_notional for l in legs]
    hedged = all(l.remaining <= l.step for l in legs) and (
        not any(filled_notionals) or (max(filled_notionals) - min(filled_notionals)) <= max(filled_notionals) * Decimal("0.03")
    )
    result = PairResult(legs=legs, hedged=hedged, elapsed_s=time.time() - start)
    log_event("pair_result", context=context, **result.summary())
    return result
