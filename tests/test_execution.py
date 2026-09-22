"""execute_pair against fake venues: passive fill, passive timeout -> cross, hedge urgency."""

from decimal import Decimal as D
from pathlib import Path

import pytest

from src.dex_perp_bot import execution
from src.dex_perp_bot.config import ExecutionConfig
from src.dex_perp_bot.execution import Leg, execute_pair


def cfg(**over):
    base = dict(hl_maker_bps=1.5, hl_taker_bps=4.5, aster_maker_bps=1.0, aster_taker_bps=3.5,
                max_breakeven_hours=8, expected_hold_hours=8, basis_window_hours=6, basis_min_samples=60,
                z_enter=1.0, z_exit=1.5, basis_exit_min_gain_bps=5, imbalance_threshold=0.3,
                passive_max_wait_s=0.2, hedge_max_wait_s=0.05, cross_cap_bps=20, poll_interval_s=0.01,
                sample_interval_s=30)
    base.update(over)
    return ExecutionConfig(**base)


class FakeVenue:
    """Book 100/100.1. Passive orders fill after `passive_fills_after` polls (None = never). IOC always fills."""

    def __init__(self, name, passive_fills_after=None, fail_submit=False):
        self.venue_name = name
        self.passive_fills_after = passive_fills_after
        self.fail_submit = fail_submit
        self.orders = {}
        self.next_id = 0
        self.cancelled = []
        self.trades_flow = [(1e12, D("100"), D("50000"), "sell"), (1e12, D("100.1"), D("50000"), "buy")]

    def get_increments(self, symbol):
        return D("0.01"), D("0.1")

    def get_book(self, symbol, depth=5):
        return {"bids": [(D("100.00"), D("5"))], "asks": [(D("100.10"), D("5"))]}

    def get_recent_trades(self, symbol, limit=100):
        import time
        now = time.time()
        return [(now - 1, p, q, s) for _, p, q, s in self.trades_flow]

    def place_limit(self, symbol, side, quantity, price, *, post_only=False, ioc=False, reduce_only=False):
        if self.fail_submit:
            raise RuntimeError("venue down")
        self.next_id += 1
        oid = str(self.next_id)
        self.orders[oid] = {"side": side, "qty": quantity, "price": price, "post_only": post_only, "ioc": ioc,
                            "polls": 0, "filled": D(0), "status": "open"}
        if ioc:
            self.orders[oid].update(filled=quantity, status="filled")
        return oid

    def get_order_state(self, symbol, order_id):
        o = self.orders[order_id]
        o["polls"] += 1
        if o["status"] == "open" and o["post_only"] and self.passive_fills_after is not None and o["polls"] >= self.passive_fills_after:
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


def test_both_passive_fill():
    a, h = FakeVenue("Aster", passive_fills_after=2), FakeVenue("Hyperliquid", passive_fills_after=2)
    legs = [Leg(a, "XUSDT", "buy", D("10")), Leg(h, "X/USDC:USDC", "sell", D("10"))]
    r = execute_pair(legs, cfg(), max_total_s=5)
    assert r.hedged
    assert all(l.current_tactic == "passive" and l.filled == D("10") for l in legs)
    assert legs[0].avg_price == D("100.00") and legs[1].avg_price == D("100.10")


def test_passive_timeout_crosses_remainder():
    a, h = FakeVenue("Aster", passive_fills_after=None), FakeVenue("Hyperliquid", passive_fills_after=None)
    legs = [Leg(a, "XUSDT", "buy", D("10")), Leg(h, "X/USDC:USDC", "sell", D("10"))]
    r = execute_pair(legs, cfg(), max_total_s=5)
    assert r.hedged
    for l in legs:
        assert l.plan.tactic == "passive"
        assert l.current_tactic == "cross" and l.crossed_after_wait and l.filled == D("10")
    assert a.cancelled and h.cancelled


def test_hedge_urgency_when_one_leg_fills():
    # Aster fills immediately, HL never fills passively -> HL must be crossed fast (hedge_max_wait_s)
    a, h = FakeVenue("Aster", passive_fills_after=1), FakeVenue("Hyperliquid", passive_fills_after=None)
    legs = [Leg(a, "XUSDT", "buy", D("10")), Leg(h, "X/USDC:USDC", "sell", D("10"))]
    r = execute_pair(legs, cfg(passive_max_wait_s=30), max_total_s=5)
    assert r.hedged and r.elapsed_s < 3
    assert legs[1].current_tactic == "cross"


def test_submit_failure_reported_not_raised():
    a, h = FakeVenue("Aster", passive_fills_after=1), FakeVenue("Hyperliquid", fail_submit=True)
    legs = [Leg(a, "XUSDT", "buy", D("10")), Leg(h, "X/USDC:USDC", "sell", D("10"))]
    r = execute_pair(legs, cfg(), max_total_s=1)
    assert not r.hedged
    assert legs[1].error and "submit" in legs[1].error
    assert legs[0].filled == D("10")


def test_imbalance_forces_cross():
    a = FakeVenue("Aster", passive_fills_after=None)
    a.get_book = lambda symbol, depth=5: {"bids": [(D("100.00"), D("50"))], "asks": [(D("100.10"), D("1"))]}
    legs = [Leg(a, "XUSDT", "buy", D("10"))]
    r = execute_pair(legs, cfg(), max_total_s=2)
    assert legs[0].plan.tactic == "cross" and legs[0].filled == D("10") and r.hedged
