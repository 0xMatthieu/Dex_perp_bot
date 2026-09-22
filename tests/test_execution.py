"""execute_pair against fake venues: anchor on the wide book, hedge crosses the tight book."""

from decimal import Decimal as D

import pytest

from src.dex_perp_bot import execution
from src.dex_perp_bot.config import ExecutionConfig
from src.dex_perp_bot.execution import Leg, execute_pair


def cfg(**over):
    base = dict(hl_maker_bps=1.5, hl_taker_bps=4.5, aster_maker_bps=1.0, aster_taker_bps=4.0,
                max_breakeven_hours=8, expected_hold_hours=8, basis_window_hours=6, basis_min_samples=60,
                z_enter=1.0, z_exit=1.5, basis_exit_min_gain_bps=5, imbalance_threshold=0.3,
                passive_max_wait_s=0.2, hedge_max_wait_s=0.05, cross_cap_bps=20, max_cross_half_spread_bps=3,
                anchor_max_wait_s=0.3, anchor_start_offset_bps=0, anchor_steps=4, repost_min_interval_s=0,
                max_tick_bps=10, max_abs_basis_bps=300, exit_max_wait_s=0.3, anchor_min_edge_bps=3, poll_interval_s=0.01, sample_interval_s=30)
    base.update(over)
    return ExecutionConfig(**base)


class FakeVenue:
    """Passive orders fill after `passive_fills_after` polls (None = never). IOC always fills at its limit."""

    def __init__(self, name, bid, ask, passive_fills_after=None, fail_submit=False, partial=None, reject_post_only=0,
                 fill_only_at_touch=False, expire_post_only=0):
        self.venue_name = name
        self.expire_post_only = expire_post_only  # accept this many post-only orders, then report them EXPIRED (Aster GTX)
        self.fill_only_at_touch = fill_only_at_touch  # passive orders away from the touch never fill
        self.reject_post_only = reject_post_only  # reject this many post-only submits first (would cross)
        self.bid, self.ask = D(bid), D(ask)
        self.passive_fills_after = passive_fills_after
        self.fail_submit = fail_submit
        self.partial = partial  # first passive fill is only this quantity, the rest on the next poll
        self.orders = {}
        self.next_id = 0
        self.cancelled = []
        self.placed = []

    def get_increments(self, symbol):
        return D("0.00001"), D("1")

    def get_book(self, symbol, depth=5):
        return {"bids": [(self.bid, D("5"))], "asks": [(self.ask, D("5"))]}

    def get_recent_trades(self, symbol, limit=100):
        import time
        now = time.time()
        return [(now - 1, self.bid, D("50000"), "sell"), (now - 1, self.ask, D("50000"), "buy")]

    def place_limit(self, symbol, side, quantity, price, *, post_only=False, ioc=False, reduce_only=False):
        if self.fail_submit:
            raise RuntimeError("venue down")
        if post_only and self.reject_post_only > 0:
            self.reject_post_only -= 1
            self.bid, self.ask = self.bid + D("0.00001"), self.ask + D("0.00001")  # touch moved
            raise RuntimeError("Aster HTTP 400: {'code': -2026, 'msg': 'Order would immediately trigger.'}")
        self.next_id += 1
        oid = str(self.next_id)
        o = {"side": side, "qty": quantity, "price": price, "post_only": post_only, "ioc": ioc,
             "polls": 0, "filled": D(0), "status": "open"}
        if ioc:
            o.update(filled=quantity, status="filled")
        if post_only and self.expire_post_only > 0:
            self.expire_post_only -= 1
            self.bid, self.ask = self.bid + D("0.00001"), self.ask + D("0.00001")  # touch moved
            o["status"] = "canceled"  # venue acknowledged the id, then expired it: nothing filled
        self.orders[oid] = o
        self.placed.append((side, quantity, price, "post_only" if post_only else "ioc"))
        return oid

    def get_order_state(self, symbol, order_id):
        o = self.orders[order_id]
        o["polls"] += 1
        # "at touch" = at or inside the spread (a resting order that is the best quote gets hit first)
        at_touch = (o["price"] >= self.bid) if o["side"] == "buy" else (o["price"] <= self.ask)
        if (o["status"] == "open" and o["post_only"] and self.passive_fills_after is not None
                and o["polls"] >= self.passive_fills_after and (at_touch or not self.fill_only_at_touch)):
            if self.partial and o["filled"] == 0 and o["qty"] > self.partial:
                o["filled"] = self.partial
            elif self.partial and o["filled"] > 0 and getattr(self, "partial_then_stall", False):
                pass  # stays partially filled forever
            else:
                o["filled"] = o["qty"]
                o["status"] = "filled"
        return {"status": o["status"], "filled": o["filled"], "avg_price": o["price"] if o["filled"] else None}

    def cancel_by_id(self, symbol, order_id):
        self.cancelled.append(order_id)
        if self.orders[order_id]["status"] == "open":
            self.orders[order_id]["status"] = "canceled"


@pytest.fixture(autouse=True)
def tmp_decision_log(tmp_path, monkeypatch):
    from src.dex_perp_bot import decision_log
    monkeypatch.setattr(decision_log, "DECISIONS_PATH", tmp_path / "d.jsonl")
    monkeypatch.setattr(execution, "log_event", lambda kind, **f: decision_log.log_event(kind, path=tmp_path / "d.jsonl", **f))
    return tmp_path / "d.jsonl"


def wide_and_tight(wide_fills=2, tight_fills=None):
    # Aster 17 bps wide (half 8.5), HL 1.2 bps wide (half 0.6) - the live CASHCAT situation
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=wide_fills)
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763", passive_fills_after=tight_fills)
    return aster, hl


def test_anchor_is_wide_book_and_hedge_crosses_tight():
    aster, hl = wide_and_tight()
    legs = [Leg(hl, "CASHCAT/USDC:USDC", "buy", D("3000")), Leg(aster, "CASHCATUSDT", "sell", D("3000"))]
    r = execute_pair(legs, cfg(anchor_max_wait_s=5), max_total_s=10)  # long horizon: first ladder step after the fill
    assert r.hedged
    a = next(l for l in legs if l.venue_name == "Aster"); h = next(l for l in legs if l.venue_name == "Hyperliquid")
    assert a.role == "anchor" and a.current_tactic == "passive" and a.avg_price == D("0.16783")  # sold at the ask
    assert h.role == "hedge" and h.current_tactic == "cross" and h.filled == D("3000")
    assert aster.placed[0][3] == "post_only" and hl.placed[0][3] == "ioc"
    # hedge was never sent before the anchor filled
    assert len(hl.placed) == 1
    assert a.maker_filled == D("3000") and h.maker_filled == 0
    assert a.est_fee_bps(cfg()) == D("1.0") and h.est_fee_bps(cfg()) == D("4.5")


def test_anchor_timeout_pays_nothing():
    aster, hl = wide_and_tight(wide_fills=None)
    legs = [Leg(hl, "X", "buy", D("3000")), Leg(aster, "Y", "sell", D("3000"))]
    r = execute_pair(legs, cfg(), max_total_s=5)
    assert not r.hedged
    assert all(l.filled == 0 for l in legs)
    assert aster.cancelled and not hl.placed  # anchor cancelled, hedge never sent
    assert any(l.error == "anchor timeout" for l in legs)


def test_partial_anchor_fill_is_hedged_in_slices():
    aster, hl = wide_and_tight(wide_fills=2)
    aster.partial = D("1500")  # first fill 50%, then the rest
    legs = [Leg(hl, "X", "buy", D("3000")), Leg(aster, "Y", "sell", D("3000"))]
    r = execute_pair(legs, cfg(), max_total_s=5)
    assert r.hedged
    h = next(l for l in legs if l.venue_name == "Hyperliquid")
    assert h.filled == D("3000") and len(hl.placed) == 2  # two hedge slices
    assert [p[1] for p in hl.placed] == [D("1500"), D("1500")]


def test_wide_book_never_crossed_even_when_planner_wants_to():
    # Adverse imbalance on the wide book: bids dominate, we want to buy there -> planner would cross,
    # but half-spread 8.5 bps > 3 bps cap -> passive.
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=2)
    aster.get_book = lambda symbol, depth=5: {"bids": [(D("0.16754"), D("50"))], "asks": [(D("0.16783"), D("1"))]}
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763")
    legs = [Leg(aster, "Y", "buy", D("3000")), Leg(hl, "X", "sell", D("3000"))]
    r = execute_pair(legs, cfg(), max_total_s=5)
    a = legs[0]
    assert a.role == "anchor" and a.plan.tactic == "passive" and "would cross" in a.plan.reason
    assert a.current_tactic == "passive" and r.hedged


def test_tight_anchor_may_cross_when_adverse():
    # Both books tight; anchor (slightly wider) has adverse imbalance -> cross allowed (half-spread <= cap).
    v1 = FakeVenue("Aster", "100.00", "100.04")  # half-spread 2 bps
    v1.get_book = lambda symbol, depth=5: {"bids": [(D("100.00"), D("50"))], "asks": [(D("100.04"), D("1"))]}
    v2 = FakeVenue("Hyperliquid", "100.00", "100.02")
    legs = [Leg(v1, "Y", "buy", D("10")), Leg(v2, "X", "sell", D("10"))]
    r = execute_pair(legs, cfg(), max_total_s=5)
    assert legs[0].role == "anchor" and legs[0].plan.tactic == "cross" and legs[0].current_tactic == "cross"
    assert r.hedged and legs[1].current_tactic == "cross"


def test_hedge_submit_failure_reported_not_raised():
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=1)
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763", fail_submit=True)
    legs = [Leg(hl, "X", "buy", D("3000")), Leg(aster, "Y", "sell", D("3000"))]
    r = execute_pair(legs, cfg(), max_total_s=1)
    assert not r.hedged
    h = next(l for l in legs if l.venue_name == "Hyperliquid")
    assert h.error and "cross" in h.error
    assert next(l for l in legs if l.venue_name == "Aster").filled == D("3000")


def test_single_leg_passive_then_cross():
    v = FakeVenue("Aster", "100.00", "100.10", passive_fills_after=None)
    legs = [Leg(v, "Y", "sell", D("10"), reduce_only=True)]
    r = execute_pair(legs, cfg(), max_total_s=3)
    assert r.hedged and legs[0].current_tactic == "cross" and legs[0].crossed_after_wait


def test_post_only_rejection_is_retried_at_new_touch():
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=1, reject_post_only=2)
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763")
    legs = [Leg(hl, "X", "buy", D("3000")), Leg(aster, "Y", "sell", D("3000"))]
    r = execute_pair(legs, cfg(), max_total_s=5)
    a = legs[1]
    assert r.hedged and a.current_tactic == "passive" and a.error is None
    assert a.avg_price == D("0.16785")  # re-posted at the moved touch, still maker
    assert a.reposts == 2


def test_post_only_rejection_gives_up_after_retries():
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=1, reject_post_only=10)
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763")
    legs = [Leg(hl, "X", "buy", D("3000")), Leg(aster, "Y", "sell", D("3000"))]
    r = execute_pair(legs, cfg(), max_total_s=2)
    assert not r.hedged and all(l.filled == 0 for l in legs) and not hl.placed
    assert legs[1].error and legs[1].error.startswith("submit:")


def test_post_only_expired_after_acceptance_is_reposted():
    """Aster GTX: the order gets an id, then shows EXPIRED because the touch moved through it (live CASHCAT 11:52 UTC)."""
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=1, expire_post_only=2)
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763")
    legs = [Leg(hl, "X", "buy", D("3000")), Leg(aster, "Y", "sell", D("3000"))]
    r = execute_pair(legs, cfg(), max_total_s=5)
    a = legs[1]
    assert r.hedged and a.current_tactic == "passive" and a.error is None
    assert a.expiries == 2 and a.reposts == 2
    assert a.avg_price == D("0.16785")  # re-posted at the moved touch, still maker
    assert not aster.cancelled  # nothing to cancel: the venue expired those orders itself


def test_post_only_expiry_gives_up_after_max():
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=1, expire_post_only=50)
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763")
    legs = [Leg(hl, "X", "buy", D("3000")), Leg(aster, "Y", "sell", D("3000"))]
    r = execute_pair(legs, cfg(), max_total_s=5)
    assert not r.hedged and all(l.filled == 0 for l in legs) and not hl.placed
    assert legs[1].expiries == execution.MAX_PASSIVE_EXPIRIES
    assert legs[1].error == f"post-only expired {execution.MAX_PASSIVE_EXPIRIES} times"


def test_anchor_ladders_from_beyond_touch_to_inside_spread():
    from src.dex_perp_bot.execution import ladder_offset_bps
    c = cfg(anchor_start_offset_bps=8, anchor_steps=4, anchor_max_wait_s=100)
    # wide book: half-spread 8.65 bps -> start 16.65 from mid, end 3 from mid (inside the spread)
    got = [round(float(ladder_offset_bps(c, t, 100, D("8.65"))), 2) for t in (0, 24, 25, 50, 75, 99)]
    assert got == [16.65, 16.65, 12.1, 7.55, 3, 3]
    # tight book: half-spread 0.6 -> start 8.6, end 3 (still beyond the touch, post-only keeps it maker)
    assert round(float(ladder_offset_bps(c, 99, 100, D("0.6"))), 2) == 3
    # Live: sell anchor starts 8 bps above the ask, fills once it steps inside the spread.
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=1, fill_only_at_touch=True)
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763")
    legs = [Leg(hl, "X", "buy", D("3000")), Leg(aster, "Y", "sell", D("3000"))]
    r = execute_pair(legs, cfg(anchor_start_offset_bps=8, anchor_steps=2, anchor_max_wait_s=0.4), max_total_s=60)
    a = legs[1]
    assert r.hedged and a.current_tactic == "passive"
    assert aster.placed[0][2] == D("0.16797")  # mid 0.167685 * (1 + 16.65 bps) rounded up
    assert aster.placed[-1][2] == D("0.16774") and a.avg_price == D("0.16774")  # mid + 3 bps, inside the spread
    assert a.reposts >= 1


def test_anchor_deep_fill_keeps_the_offset():
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=1)  # fills anywhere
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763")
    legs = [Leg(hl, "X", "buy", D("3000")), Leg(aster, "Y", "sell", D("3000"))]
    r = execute_pair(legs, cfg(anchor_start_offset_bps=8, anchor_steps=4, anchor_max_wait_s=5), max_total_s=60)
    assert r.hedged and legs[1].avg_price == D("0.16797") and legs[1].maker_filled == D("3000")


def test_coarse_tick_rests_at_touch_not_a_full_tick_away():
    from src.dex_perp_bot.execution import passive_price
    # HMSTR-like: price 0.000178, tick 0.000001 = 56 bps. An 8 bps offset must not become 56 bps.
    bids, asks = [(D("0.000178"), D("1"))], [(D("0.000179"), D("1"))]
    assert passive_price("sell", bids, asks, D("8"), D("0.000001")) == D("0.000179")
    assert passive_price("buy", bids, asks, D("8"), D("0.000001")) == D("0.000178")
    # fine tick: offset applies
    bids, asks = [(D("0.16754"), D("1"))], [(D("0.16783"), D("1"))]
    assert passive_price("sell", bids, asks, D("8"), D("0.00001")) == D("0.16782")  # mid + 8 bps, inside the spread


def test_abort_cancels_anchor_and_hedges_partial():
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=2)
    aster.partial = D("1000"); aster.partial_then_stall = True  # fills 1000, then nothing more
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763")
    legs = [Leg(hl, "X", "buy", D("3000")), Leg(aster, "Y", "sell", D("3000"))]
    flag = {"n": 0}
    def should_abort():
        flag["n"] += 1
        return flag["n"] > 3
    r = execute_pair(legs, cfg(anchor_max_wait_s=5), max_total_s=10, should_abort=should_abort)
    a = legs[1]; h = legs[0]
    assert a.error == "aborted by control" and aster.cancelled
    assert h.filled == a.filled  # whatever filled on the anchor got hedged
    assert not r.hedged or a.filled == D("3000")


def test_exit_mode_no_ladder():
    aster = FakeVenue("Aster", "0.16754", "0.16783", passive_fills_after=1)
    hl = FakeVenue("Hyperliquid", "0.16761", "0.16763")
    legs = [Leg(hl, "X", "sell", D("3000"), reduce_only=True), Leg(aster, "Y", "buy", D("3000"), reduce_only=True)]
    r = execute_pair(legs, cfg(anchor_start_offset_bps=8, anchor_steps=4), max_total_s=5, ladder=False, anchor_wait_s=0.3)
    assert r.hedged and aster.placed[0][2] == D("0.16754")  # at the bid, no offset


def test_passive_price_inside_spread_never_crosses():
    from src.dex_perp_bot.execution import passive_price
    bids, asks = [(D("0.16754"), D("1"))], [(D("0.16783"), D("1"))]
    # 3 bps from mid, inside the spread, still above the bid
    assert passive_price("sell", bids, asks, D("3"), D("0.00001")) == D("0.16774")
    assert passive_price("buy", bids, asks, D("3"), D("0.00001")) == D("0.16763")
    # an offset smaller than a tick can never end up at or through the other side
    assert passive_price("sell", bids, asks, D("0"), D("0.00001")) >= D("0.16755")
    assert passive_price("buy", bids, asks, D("0"), D("0.00001")) <= D("0.16782")
    # at the half-spread = the touch
    hs = D("8.647")
    assert passive_price("sell", bids, asks, hs, D("0.00001")) == D("0.16783")
