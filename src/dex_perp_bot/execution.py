"""Two-leg execution: passive on the wide book first, then hedge by crossing the tight book.

Sequenced plan (``execute_pair``):

1. Read both books. The leg on the venue with the **wider half-spread** is the *anchor*;
   the other is the *hedge*.
2. The anchor is worked passively (post-only at the touch, re-posted when the touch moves
   away) for up to ``anchor_max_wait_s``. It only crosses if the planner says so *and* the
   half-spread is at most ``max_cross_half_spread_bps`` (crossing a wide book pays the
   spread, which dwarfs any timing edge).
3. Every time the anchor's filled quantity grows, the hedge leg is sent as an IOC limit for
   the new quantity (slippage capped at ``cross_cap_bps``). On a tight book this costs about
   half a spread plus the taker fee and the naked exposure lasts seconds. Small partial fills
   are batched until they are worth hedging, unless the anchor is done.
4. If the anchor never fills, it is cancelled and nothing was paid.

Every step is written to the decision log so fills can be audited later.
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Callable, Dict, List, Optional

from .config import ExecutionConfig
from .decision_log import log_event
from .exchanges.base import DexAPIError
from .microstructure import LegPlan, cross_price, half_spread_bps, plan_leg, round_to_tick, slippage_bps

logger = logging.getLogger(__name__)

MIN_HEDGE_SLICE_FRACTION = Decimal("0.2")  # hedge partial fills once they reach 20% of the leg
MAX_REPOSTS = 200  # stop chasing the touch after this many re-posts; stay resting instead
POST_ONLY_RETRIES = 3  # a post-only that would cross is rejected; re-read the book and try again
_POST_ONLY_REJECT_MARKERS = ("-2026", "immediately", "post only", "post-only", "postonly", "alo", "would cross", "gtx")


def is_post_only_rejection(exc: Exception) -> bool:
    msg = str(exc).lower()
    return any(m in msg for m in _POST_ONLY_REJECT_MARKERS)


@dataclass
class Leg:
    venue: Any  # AsterClient | HyperliquidClient (both expose the execution primitives)
    symbol: str
    side: str  # "buy" | "sell"
    quantity: Decimal
    reduce_only: bool = False
    # filled in during execution
    role: str = ""  # "anchor" | "hedge"
    plan: Optional[LegPlan] = None
    reference_mid: Decimal = Decimal(0)
    half_spread_bps: Decimal = Decimal(0)
    tick: Decimal = Decimal(0)
    step: Decimal = Decimal(0)
    order_id: Optional[str] = None
    order_price: Optional[Decimal] = None
    current_tactic: str = ""
    filled: Decimal = Decimal(0)
    fill_notional: Decimal = Decimal(0)
    maker_filled: Decimal = Decimal(0)
    placed_at: float = 0.0
    first_fill_at: Optional[float] = None
    deadline: float = math.inf
    done: bool = False
    crossed_after_wait: bool = False
    cross_attempts: int = 0
    reposts: int = 0
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

    def est_fee_bps(self, cfg: ExecutionConfig) -> Optional[Decimal]:
        """Blended fee estimate from the maker/taker split of the fills."""
        if self.filled <= 0:
            return None
        maker = self.maker_filled / self.filled
        return maker * Decimal(str(cfg.fee_bps(self.venue_name, True))) + (1 - maker) * Decimal(str(cfg.fee_bps(self.venue_name, False)))


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
                    "venue": l.venue_name, "symbol": l.symbol, "side": l.side, "role": l.role, "qty": l.quantity,
                    "filled": l.filled, "avg_price": l.avg_price, "tactic": l.current_tactic,
                    "planned": l.plan.tactic if l.plan else None, "half_spread_bps": l.half_spread_bps,
                    "error": l.error,
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


def passive_price(side: str, bids: List, asks: List, offset_bps: Decimal, tick: Decimal) -> Decimal:
    """Touch improved by ``offset_bps`` in our favour: BUY below the bid, SELL above the ask. Never crosses.

    When one tick is coarser than the requested offset (low-priced coins), the offset would round to a
    whole tick, i.e. far more than asked, so we rest at the touch instead.
    """
    touch = bids[0][0] if side == "buy" else asks[0][0]
    if offset_bps <= 0 or touch <= 0:
        return round_to_tick(touch, tick, side)
    tick_bps = tick / touch * Decimal(10_000)
    if tick_bps >= offset_bps:
        return round_to_tick(touch, tick, side)
    raw = touch * (1 - offset_bps / Decimal(10_000)) if side == "buy" else touch * (1 + offset_bps / Decimal(10_000))
    return round_to_tick(raw, tick, side)


def _submit(leg: Leg, tactic: str, cfg: ExecutionConfig, context: str, qty: Optional[Decimal] = None,
            offset_bps: Decimal = Decimal(0)) -> None:
    """Place (or re-place) ``qty`` (default: the remaining quantity) of a leg with the given tactic."""
    book = leg.venue.get_book(leg.symbol, depth=5)
    bids, asks = book["bids"], book["asks"]
    if not bids or not asks:
        raise DexAPIError(f"Empty book for {leg.symbol} on {leg.venue_name}")
    qty = _round_qty(qty if qty is not None else leg.remaining, leg.step)
    if qty <= 0:
        return
    if tactic == "passive":
        attempt = 0
        while True:
            price = passive_price(leg.side, bids, asks, offset_bps, leg.tick)
            try:
                order_id = leg.venue.place_limit(leg.symbol, leg.side, qty, price, post_only=True, reduce_only=leg.reduce_only)
                break
            except Exception as exc:
                attempt += 1
                if not is_post_only_rejection(exc) or attempt > POST_ONLY_RETRIES:
                    raise
                # The touch moved through our price between the book read and the send: refresh and re-post.
                logger.info("[%s] post-only %s @ %s rejected (would cross); re-reading book (attempt %d/%d)",
                            leg.venue_name, leg.side.upper(), price, attempt, POST_ONLY_RETRIES)
                log_event("note", message="post-only rejected, re-posting at new touch", venue=leg.venue_name,
                          symbol=leg.symbol, side=leg.side, price=price, attempt=attempt, context=context)
                book = leg.venue.get_book(leg.symbol, depth=5)
                bids, asks = book["bids"], book["asks"]
                if not bids or not asks:
                    raise DexAPIError(f"Empty book for {leg.symbol} on {leg.venue_name}")
        leg.reposts += attempt
    else:
        price = cross_price(leg.side, bids[0][0], asks[0][0], Decimal(str(cfg.cross_cap_bps)), leg.tick)
        order_id = leg.venue.place_limit(leg.symbol, leg.side, qty, price, ioc=True, reduce_only=leg.reduce_only)
        leg.cross_attempts += 1
    leg.order_id = order_id
    leg.order_price = price
    leg.current_tactic = tactic
    leg.placed_at = leg.placed_at or time.time()
    logger.info("[%s] %s %s %s @ %s (%s %s, %s) order %s", leg.venue_name, leg.side.upper(), qty, leg.symbol, price,
                leg.role, tactic, context, order_id)
    log_event("leg_order", venue=leg.venue_name, symbol=leg.symbol, side=leg.side, role=leg.role, qty=qty, price=price,
              tactic=tactic, context=context, order_id=order_id, best_bid=bids[0][0], best_ask=asks[0][0],
              half_spread_bps=half_spread_bps(bids, asks), offset_bps=offset_bps)


def _refresh(leg: Leg) -> str:
    """Poll the venue and fold new fills into the leg. Returns status open|filled|canceled."""
    if leg.order_id is None:
        return "canceled"
    state = leg.venue.get_order_state(leg.symbol, leg.order_id)
    filled_now = state.get("filled") or Decimal(0)
    avg = state.get("avg_price")
    prev = sum((f["delta"] for f in leg.fills_log if f["order_id"] == leg.order_id), Decimal(0))
    delta = filled_now - prev
    if delta > 0:
        if not avg:  # last resort so a fill is never dropped from the accounting
            avg = leg.order_price or leg.reference_mid
            logger.warning("[%s] order %s filled %s without an average price; using order price", leg.venue_name, leg.order_id, delta)
        leg.filled += delta
        leg.fill_notional += delta * avg
        if leg.current_tactic == "passive":
            leg.maker_filled += delta
        leg.first_fill_at = leg.first_fill_at or time.time()
        leg.fills_log.append({"order_id": leg.order_id, "delta": delta, "avg_price": avg, "tactic": leg.current_tactic})
    status = state.get("status", "open")
    if leg.remaining <= 0:
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
    leg.order_id = None


def _finish_leg(leg: Leg, cfg: ExecutionConfig, context: str) -> None:
    wait_s = ((leg.first_fill_at or time.time()) - leg.placed_at) if leg.placed_at else None
    slip = slippage_bps(leg.side, leg.avg_price, leg.reference_mid) if leg.avg_price else None
    log_event(
        "leg_fill", venue=leg.venue_name, symbol=leg.symbol, side=leg.side, role=leg.role, qty=leg.quantity,
        filled=leg.filled, avg_price=leg.avg_price, reference_mid=leg.reference_mid, slippage_bps=slip,
        est_fee_bps=leg.est_fee_bps(cfg), maker_fraction=(leg.maker_filled / leg.filled) if leg.filled else None,
        wait_s=wait_s, planned_tactic=leg.plan.tactic if leg.plan else None, final_tactic=leg.current_tactic,
        crossed_after_wait=leg.crossed_after_wait, cross_attempts=leg.cross_attempts, reposts=leg.reposts,
        half_spread_bps=leg.half_spread_bps, imbalance=leg.plan.imbalance if leg.plan else None,
        queue_qty=leg.plan.queue_qty if leg.plan else None,
        expected_wait_s=leg.plan.expected_wait_s if leg.plan else None, error=leg.error, context=context,
    )


def _plan(leg: Leg, cfg: ExecutionConfig, context: str, now: float) -> None:
    try:
        leg.tick, leg.step = leg.venue.get_increments(leg.symbol)
        book = leg.venue.get_book(leg.symbol, depth=5)
        trades = leg.venue.get_recent_trades(leg.symbol, limit=100)
        leg.reference_mid = _mid(book)
        leg.half_spread_bps = half_spread_bps(book["bids"], book["asks"])
        leg.plan = plan_leg(
            leg.side, book["bids"], book["asks"], trades, now_s=now,
            imbalance_threshold=Decimal(str(cfg.imbalance_threshold)), max_wait_s=cfg.passive_max_wait_s,
            max_cross_half_spread_bps=Decimal(str(cfg.max_cross_half_spread_bps)),
        )
    except Exception as exc:
        leg.error = f"plan: {exc}"
        leg.plan = LegPlan("passive", f"planning failed ({exc})", Decimal(0), Decimal(0), Decimal(0), 0.0)
        logger.warning("[%s] planning %s failed: %s", leg.venue_name, leg.symbol, exc)


def _hedge_slice(anchor: Leg, hedge: Leg, cfg: ExecutionConfig, context: str) -> None:
    """Cross the hedge leg for whatever the anchor has filled and we have not hedged yet."""
    target = min(anchor.filled, hedge.quantity)
    outstanding = target - hedge.filled
    slice_qty = _round_qty(outstanding, hedge.step)
    if slice_qty <= 0:
        return
    if not anchor.done and slice_qty < hedge.quantity * MIN_HEDGE_SLICE_FRACTION:
        return  # batch small partials until they are worth a taker order
    if hedge.cross_attempts >= 6:
        hedge.error = "cross attempts exhausted"
        return
    hedge.crossed_after_wait = False
    _submit(hedge, "cross", cfg, f"{context}:hedge", qty=slice_qty)
    # IOC resolves immediately; fold the result in now.
    try:
        _refresh(hedge)
    except Exception as exc:
        logger.warning("[%s] hedge refresh failed: %s", hedge.venue_name, exc)


def ladder_offset_bps(cfg: ExecutionConfig, elapsed_s: float, horizon_s: float) -> Decimal:
    """Offset beyond the touch as a function of time: start at ``anchor_start_offset_bps`` and step down
    linearly to 0 (the touch) over ``anchor_steps`` equal slices of ``horizon_s``."""
    steps = max(1, cfg.anchor_steps)
    if horizon_s <= 0 or cfg.anchor_start_offset_bps <= 0 or steps == 1:
        return Decimal(0)
    k = min(steps - 1, int(elapsed_s / (horizon_s / steps)))
    # k = 0 -> full offset, k = steps-1 (last slice) -> at the touch
    return Decimal(str(cfg.anchor_start_offset_bps)) * Decimal(steps - 1 - k) / Decimal(steps - 1)


def execute_pair(
    legs: List[Leg],
    cfg: ExecutionConfig,
    *,
    context: str = "entry",
    max_total_s: float = 300.0,
    ladder: bool = True,
    anchor_wait_s: Optional[float] = None,
    should_abort: Optional[Callable[[], bool]] = None,
    on_tick: Optional[Callable[[], None]] = None,
) -> PairResult:
    """Run both legs (or a single leg) to completion. Never raises; inspect ``PairResult``.

    ``ladder``        rest the anchor beyond the touch and tighten over time (entries); False = at the touch (exits)
    ``anchor_wait_s`` override the anchor patience (defaults to cfg.anchor_max_wait_s)
    ``should_abort``  polled every loop; True cancels the anchor, hedges whatever filled and returns
    ``on_tick``       called about every 30 s while working (status snapshot, keeps the dashboard alive)
    """
    start = time.time()
    last_tick = start
    for leg in legs:
        _plan(leg, cfg, context, start)

    if len(legs) == 1:
        legs[0].role = "anchor"
        _work_single(legs[0], cfg, context, max_total_s)
        _finish_leg(legs[0], cfg, context)
        result = PairResult(legs=legs, hedged=legs[0].remaining <= legs[0].step, elapsed_s=time.time() - start)
        log_event("pair_result", context=context, **result.summary())
        return result

    # Anchor = wider half-spread (ties -> first leg). Hedge = the other one.
    anchor, hedge = sorted(legs, key=lambda l: l.half_spread_bps, reverse=True)[:2]
    anchor.role, hedge.role = "anchor", "hedge"
    log_event("leg_plan", venue=anchor.venue_name, symbol=anchor.symbol, side=anchor.side, role="anchor",
              qty=anchor.quantity, tactic=anchor.plan.tactic, reason=anchor.plan.reason, imbalance=anchor.plan.imbalance,
              queue_qty=anchor.plan.queue_qty, flow_rate=anchor.plan.flow_rate, expected_wait_s=anchor.plan.expected_wait_s,
              half_spread_bps=anchor.half_spread_bps, mid=anchor.reference_mid, context=context)
    log_event("leg_plan", venue=hedge.venue_name, symbol=hedge.symbol, side=hedge.side, role="hedge",
              qty=hedge.quantity, tactic="cross", reason=f"hedge on the tighter book (half-spread {hedge.half_spread_bps:.1f} bps vs anchor {anchor.half_spread_bps:.1f} bps)",
              imbalance=hedge.plan.imbalance, half_spread_bps=hedge.half_spread_bps, mid=hedge.reference_mid, context=context)

    # 1. Work the anchor. Passive anchors ladder in from ``anchor_start_offset_bps`` beyond the touch
    #    down to the touch over the horizon (bounded by the time we have in this window).
    tactic = anchor.plan.tactic if anchor.plan else "passive"
    horizon = min(anchor_wait_s if anchor_wait_s is not None else cfg.anchor_max_wait_s, max(max_total_s - 30.0, 30.0))
    ladder_start = time.time()
    last_repost = 0.0
    offset_now = (lambda elapsed: ladder_offset_bps(cfg, elapsed, horizon)) if ladder else (lambda elapsed: Decimal(0))
    try:
        _submit(anchor, tactic, cfg, context, offset_bps=offset_now(0.0) if tactic == "passive" else Decimal(0))
        anchor.deadline = time.time() + (horizon if tactic == "passive" else max(cfg.poll_interval_s * 2, 5.0))
    except Exception as exc:
        anchor.error = f"submit: {exc}"
        logger.error("[%s] anchor submit failed: %s", anchor.venue_name, exc)

    while time.time() - start < max_total_s and not anchor.error:
        if on_tick and time.time() - last_tick >= 30:
            try:
                on_tick()
            except Exception as exc:
                logger.debug("on_tick failed: %s", exc)
            last_tick = time.time()
        if should_abort and should_abort():
            _cancel(anchor)
            anchor.error = "aborted by control"
            log_event("note", message="execution aborted by safety switch; anchor cancelled", venue=anchor.venue_name,
                      symbol=anchor.symbol, filled=anchor.filled, context=context)
            break
        try:
            status = _refresh(anchor)
        except Exception as exc:
            logger.warning("[%s] poll failed: %s", anchor.venue_name, exc)
            time.sleep(cfg.poll_interval_s)
            continue
        # 2. Hedge whatever is filled so far.
        if anchor.filled > hedge.filled:
            try:
                _hedge_slice(anchor, hedge, cfg, context)
            except Exception as exc:
                hedge.error = f"cross: {exc}"
                logger.error("[%s] hedge failed: %s", hedge.venue_name, exc)
        if anchor.done:
            break
        now = time.time()
        if status == "canceled":  # IOC remainder or venue-side cancel
            if anchor.current_tactic == "cross" and anchor.cross_attempts < 4 and anchor.half_spread_bps <= Decimal(str(cfg.max_cross_half_spread_bps)):
                try:
                    _submit(anchor, "cross", cfg, f"{context}:reprice")
                    anchor.deadline = now + max(cfg.poll_interval_s * 2, 5.0)
                except Exception as exc:
                    anchor.error = f"cross: {exc}"
            else:
                anchor.order_id = None
                anchor.error = anchor.error or "order cancelled by venue"
                break
        elif now >= anchor.deadline:
            _cancel(anchor)
            anchor.error = "anchor timeout"
            break
        elif anchor.current_tactic == "passive" and now - last_repost >= cfg.repost_min_interval_s:
            # Re-post when our resting price drifts a tick or more from the ladder target
            # (the touch moved, or the ladder stepped closer to the touch).
            try:
                book = anchor.venue.get_book(anchor.symbol, depth=1)
                offset = offset_now(now - ladder_start)
                target = passive_price(anchor.side, book["bids"], book["asks"], offset, anchor.tick)
                if anchor.order_price is not None and abs(target - anchor.order_price) >= anchor.tick and anchor.reposts < MAX_REPOSTS:
                    _cancel(anchor)
                    if not anchor.done:
                        anchor.reposts += 1
                        last_repost = now
                        _submit(anchor, "passive", cfg, f"{context}:repost", offset_bps=offset)
            except Exception as exc:
                logger.warning("[%s] repost check failed: %s", anchor.venue_name, exc)
        time.sleep(cfg.poll_interval_s)

    # 3. Final hedge for anything filled but not yet hedged; cancel anything resting.
    if anchor.order_id and not anchor.done:
        _cancel(anchor)
    if anchor.filled > hedge.filled:
        for _ in range(3):
            try:
                _hedge_slice(anchor, hedge, cfg, f"{context}:final")
            except Exception as exc:
                hedge.error = f"cross: {exc}"
                break
            if hedge.filled >= min(anchor.filled, hedge.quantity) - hedge.step:
                break
            time.sleep(cfg.poll_interval_s)
    if anchor.error == "anchor timeout" and anchor.filled == 0:
        log_event("note", message="anchor never filled; entry abandoned without fees", venue=anchor.venue_name,
                  symbol=anchor.symbol, context=context, waited_s=round(time.time() - start, 1))

    for leg in legs:
        leg.done = True
        _finish_leg(leg, cfg, context)

    notionals = [anchor.fill_notional, hedge.fill_notional]
    hedged = anchor.filled > 0 and abs(anchor.fill_notional - hedge.fill_notional) <= max(notionals) * Decimal("0.03") \
        and all(l.remaining <= l.step for l in legs)
    result = PairResult(legs=legs, hedged=hedged, elapsed_s=time.time() - start)
    log_event("pair_result", context=context, **result.summary())
    return result


def _work_single(leg: Leg, cfg: ExecutionConfig, context: str, max_total_s: float) -> None:
    """One leg on its own (e.g. closing a lone position): passive with patience, then cross."""
    start = time.time()
    tactic = leg.plan.tactic if leg.plan else "passive"
    try:
        _submit(leg, tactic, cfg, context)
        leg.deadline = time.time() + (cfg.passive_max_wait_s if tactic == "passive" else 5.0)
    except Exception as exc:
        leg.error = f"submit: {exc}"
        return
    while time.time() - start < max_total_s and not leg.done:
        try:
            status = _refresh(leg)
        except Exception as exc:
            logger.warning("[%s] poll failed: %s", leg.venue_name, exc)
            time.sleep(cfg.poll_interval_s)
            continue
        if leg.done:
            break
        if status == "canceled" or time.time() >= leg.deadline:
            if leg.current_tactic == "passive":
                _cancel(leg)
                leg.crossed_after_wait = True
            if leg.done:
                break
            if leg.cross_attempts >= 4:
                leg.error = "cross attempts exhausted"
                break
            try:
                _submit(leg, "cross", cfg, f"{context}:reprice")
                leg.deadline = time.time() + 5.0
            except Exception as exc:
                leg.error = f"cross: {exc}"
                break
        time.sleep(cfg.poll_interval_s)
    if leg.order_id and not leg.done:
        _cancel(leg)
