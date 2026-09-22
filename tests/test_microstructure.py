import math
from decimal import Decimal as D

import pytest

from src.dex_perp_bot.microstructure import (
    aggressive_flow_rate, basis_bps, basis_gain_bps, basis_stats, breakeven_hours, cross_price,
    expected_reversion_bps, expected_wait_s, favorable_z, funding_bps_per_hour, imbalance, plan_leg,
    queue_ahead, round_to_tick, slippage_bps,
)

BIDS = [(D("100.0"), D("10")), (D("99.9"), D("5"))]
ASKS = [(D("100.1"), D("2")), (D("100.2"), D("3"))]


def test_imbalance_sign_and_range():
    assert imbalance(BIDS, ASKS) == D("10")/D("20")  # (15-5)/20 = 0.5
    assert imbalance([], []) == 0
    assert imbalance([], ASKS) == -1


def test_queue_and_wait():
    assert queue_ahead(BIDS, D("100.0")) == D("10")
    assert queue_ahead(BIDS, D("50")) == 0
    trades = [(100.0, D("100"), D("4"), "sell"), (110.0, D("100"), D("2"), "buy"), (10.0, D("100"), D("99"), "sell")]
    rate = aggressive_flow_rate(trades, "sell", window_s=60, now_s=120.0)  # only the ts=100 trade
    assert rate == D("4") / D("60")
    assert expected_wait_s(D("10"), rate) == pytest.approx(150.0)
    assert expected_wait_s(D("10"), D(0)) == math.inf


def test_basis_and_z():
    assert basis_bps(D("101"), D("100")) == D("100")
    hist = [D(x) for x in (0, 0, 0, 10, 10, 10)]
    st = basis_stats(hist, current_bps=D("15"), min_samples=6)
    assert st.n == 6 and st.mean_bps == D("5") and st.z is not None and st.z > 0
    # Long Aster profits when basis rises; basis is currently rich -> unfavorable
    assert favorable_z(st, "Aster") < 0
    assert favorable_z(st, "Hyperliquid") > 0
    assert expected_reversion_bps(st, "Aster") == D("-10")
    assert expected_reversion_bps(st, "Hyperliquid") == D("10")
    assert basis_stats(hist[:2], D("1"), min_samples=6).z is None
    assert basis_gain_bps(D("0"), D("8"), "Aster") == D("8")
    assert basis_gain_bps(D("0"), D("8"), "Hyperliquid") == D("-8")


def test_breakeven():
    assert funding_bps_per_hour(D("100")) == pytest.approx(D("1.1415"), abs=D("0.001"))
    assert breakeven_hours(D("10.5"), D("100")) == pytest.approx(9.2, abs=0.05)
    assert breakeven_hours(D("10"), D("0")) == math.inf
    assert breakeven_hours(D("-3"), D("50")) == 0.0


def test_plan_leg_tactics():
    now = 1000.0
    trades = [(now - 10, D("100"), D("1"), "sell")]  # 1 unit / 120 s of sell flow
    # BUY with buyers dominating (imbalance +0.5 >= 0.3) -> cross
    p = plan_leg("buy", BIDS, ASKS, trades, now_s=now, imbalance_threshold=D("0.3"), max_wait_s=90)
    assert p.tactic == "cross" and "imbalance" in p.reason
    # SELL with buyers dominating is fine to rest, but queue 2 at ask / buy flow 0 -> infinite wait -> cross
    p = plan_leg("sell", BIDS, ASKS, trades, now_s=now, imbalance_threshold=D("0.3"), max_wait_s=90)
    assert p.tactic == "cross" and "queue" in p.reason
    # SELL with buy flow so wait is short -> passive
    trades2 = [(now - 5, D("100.1"), D("10"), "buy")]
    p = plan_leg("sell", BIDS, ASKS, trades2, now_s=now, imbalance_threshold=D("0.3"), max_wait_s=90)
    assert p.tactic == "passive"
    # BUY with neutral book and fast sell flow -> passive
    p = plan_leg("buy", BIDS, ASKS, [(now - 5, D("100"), D("100"), "sell")], now_s=now, imbalance_threshold=D("0.6"), max_wait_s=90)
    assert p.tactic == "passive"


def test_price_helpers():
    assert round_to_tick(D("100.123"), D("0.01"), "buy") == D("100.12")
    assert round_to_tick(D("100.123"), D("0.01"), "sell") == D("100.13")
    assert cross_price("buy", D("100"), D("100.1"), D("20"), D("0.01")) == D("100.31")   # ask * 1.002 rounded up
    assert cross_price("sell", D("100"), D("100.1"), D("20"), D("0.01")) == D("99.80")   # bid * 0.998 rounded down
    assert slippage_bps("buy", D("100.1"), D("100")) == D("10")
    assert slippage_bps("sell", D("100.1"), D("100")) == D("-10")
