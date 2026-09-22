"""Funding scan (next-hour vs steady APY, delisted filter) and the entry gate (windfall break-even, basis sanity)."""

import time
from decimal import Decimal as D
from pathlib import Path

import pytest

from src.dex_perp_bot import funding, strategy
from src.dex_perp_bot.basis import BasisTracker
from src.dex_perp_bot.config import ExecutionConfig
from src.dex_perp_bot.exchanges.base import DexAPIError
from src.dex_perp_bot.funding import FundingComparison
from src.dex_perp_bot.microstructure import breakeven_hours, funding_bps_per_hour


def cfg(**over):
    base = dict(hl_maker_bps=1.5, hl_taker_bps=4.5, aster_maker_bps=0.0, aster_taker_bps=4.0,
                max_breakeven_hours=8, expected_hold_hours=8, basis_window_hours=6, basis_min_samples=60,
                z_enter=1.0, z_exit=1.5, basis_exit_min_gain_bps=5, imbalance_threshold=0.3,
                passive_max_wait_s=0.2, hedge_max_wait_s=0.05, cross_cap_bps=20, max_cross_half_spread_bps=3,
                anchor_max_wait_s=0.3, anchor_start_offset_bps=0, anchor_steps=4, repost_min_interval_s=0,
                max_tick_bps=10, max_abs_basis_bps=300, exit_max_wait_s=0.3, anchor_min_edge_bps=3,
                poll_interval_s=0.01, sample_interval_s=30)
    base.update(over)
    return ExecutionConfig(**base)


def opp(symbol="ATOM", long_venue="Hyperliquid", next_hour="206", steady="196"):
    short = "Aster" if long_venue == "Hyperliquid" else "Hyperliquid"
    return FundingComparison(
        symbol=symbol, long_venue=long_venue, short_venue=short,
        apy_difference=D(next_hour), apy_difference_basis="aster 8h",
        apy_aster_1h=D(0), apy_aster_4h=D(0), apy_hyperliquid_1h=D(0), apy_hyperliquid_4h=D(0),
        rate_aster=D(0), rate_hyperliquid=D(0), funding_is_imminent=True, next_funding_time_ms=None,
        long_max_leverage=5, short_max_leverage=20, is_actionable=True, apy_steady=D(steady),
    )


# ---------------------------------------------------------------------------
# break-even with a first-hour windfall
# ---------------------------------------------------------------------------

def test_breakeven_windfall_repays_within_first_hour():
    # 120% next hour = 1.37 bps; cost 1 bps repaid in a fraction of the first hour
    assert breakeven_hours(D("1"), D("10"), D("120")) == pytest.approx(0.73, abs=0.01)


def test_breakeven_windfall_then_steady():
    # next hour pays 1.37 bps of a 10 bps cost, steady 100% APY = 1.1415 bps/h for the remaining 8.63 bps
    hours = breakeven_hours(D("10"), D("100"), D("120"))
    expected = 1 + float((D("10") - funding_bps_per_hour(D("120"))) / funding_bps_per_hour(D("100")))
    assert hours == pytest.approx(expected, abs=1e-6)
    assert hours > breakeven_hours(D("10"), D("120"))  # the old (all-windfall) model was too optimistic


def test_breakeven_windfall_but_no_steady_income_is_infinite():
    assert breakeven_hours(D("10"), D("0"), D("50")) == float("inf")
    assert breakeven_hours(D("10"), D("-20"), D("50")) == float("inf")


def test_breakeven_two_arg_form_unchanged():
    assert breakeven_hours(D("10.5"), D("100")) == pytest.approx(9.2, abs=0.05)
    assert breakeven_hours(D("-3"), D("50"), D("500")) == 0.0


# ---------------------------------------------------------------------------
# scan: steady vs next-hour, delisted filter
# ---------------------------------------------------------------------------

class FakeAster:
    def __init__(self, rates, intervals):
        self._rates, self._intervals = rates, intervals

    def get_funding_rate(self):
        return self._rates

    def get_funding_info(self):
        return self._intervals

    def get_max_leverage(self, symbol):
        return 20


class FakeHL:
    def __init__(self, rates, delisted=()):
        self._rates, self._delisted = rates, set(delisted)

    def get_predicted_funding_rates(self):
        return self._rates

    def get_delisted_coins(self):
        return self._delisted

    def get_max_leverage(self, symbol):
        return 5


def _hl(symbol, rate):
    return [symbol, [["HlPerp", {"fundingRate": rate}]]]


def test_scan_counts_imminent_aster_settlement_whole_but_reports_steady_rate():
    soon = int(time.time() * 1000) + 10 * 60 * 1000
    aster = FakeAster([{"symbol": "ATOMUSDT", "lastFundingRate": "0.0001", "nextFundingTime": soon}], {"ATOMUSDT": 8})
    hl = FakeHL([_hl("ATOM", "-0.0002")])
    [o] = funding.fetch_and_compare_funding_rates(aster, hl, imminent_funding_minutes=60)
    assert o.long_venue == "Hyperliquid"
    hl_apy = D("-0.0002") * 24 * 365 * 100          # longs receive negative funding -> +175.2%
    aster_8h_apy = D("0.0001") * 3 * 365 * 100      # 10.95% paid whole within the hour
    assert o.apy_next_hour == aster_8h_apy - hl_apy
    assert o.apy_steady == aster_8h_apy / 8 - hl_apy
    assert o.apy_next_hour > o.apy_steady
    assert o.apy_difference_basis == "aster 8h"


def test_scan_not_imminent_next_hour_equals_steady():
    later = int(time.time() * 1000) + 5 * 3600 * 1000
    aster = FakeAster([{"symbol": "ATOMUSDT", "lastFundingRate": "0.0001", "nextFundingTime": later}], {"ATOMUSDT": 8})
    hl = FakeHL([_hl("ATOM", "-0.0002")])
    [o] = funding.fetch_and_compare_funding_rates(aster, hl, imminent_funding_minutes=60)
    assert o.apy_next_hour == o.apy_steady


def test_scan_drops_hyperliquid_delisted_coins():
    soon = int(time.time() * 1000) + 10 * 60 * 1000
    aster = FakeAster([
        {"symbol": "AIUSDT", "lastFundingRate": "0.00048", "nextFundingTime": soon},
        {"symbol": "ATOMUSDT", "lastFundingRate": "0.0001", "nextFundingTime": soon},
    ], {"AIUSDT": 1, "ATOMUSDT": 8})
    hl = FakeHL([_hl("AI", "0.0"), _hl("ATOM", "-0.0002")], delisted={"AI"})
    out = funding.fetch_and_compare_funding_rates(aster, hl, imminent_funding_minutes=60)
    assert [o.symbol for o in out] == ["ATOM"]
    assert funding.LAST_GATE_REASONS == {}


# ---------------------------------------------------------------------------
# gate: basis sanity, empty books
# ---------------------------------------------------------------------------

def test_gate_rejects_mismatched_contract_basis(tmp_path: Path):
    tracker = BasisTracker(path=tmp_path / "basis.json")
    snap = {"basis_bps": D("441435"), "half_spread_aster_bps": D("56"), "half_spread_hl_bps": D("8"),
            "tick_aster_bps": D("1"), "tick_hl_bps": D("1")}
    info = strategy.evaluate_entry_gate(opp("MEME", next_hour="264", steady="264"), cfg(), tracker, snap["basis_bps"], snap)
    assert info["ok"] is False
    assert info["reason_code"] == "basis_out_of_range"


def test_gate_uses_windfall_then_steady_breakeven(tmp_path: Path):
    tracker = BasisTracker(path=tmp_path / "basis.json")
    snap = {"basis_bps": D("0"), "half_spread_aster_bps": D("1"), "half_spread_hl_bps": D("1"),
            "tick_aster_bps": D("1"), "tick_hl_bps": D("1")}
    # DASH-like: 113% next hour (8h payment counted whole), ~14% steady. Fees 10 bps + 2 bps crossing.
    info = strategy.evaluate_entry_gate(opp("DASH", "Aster", next_hour="113", steady="14"), cfg(), tracker, D("0"), snap)
    assert info["apy_next_hour_pct"] == D("113") and info["apy_steady_pct"] == D("14")
    assert info["breakeven_hours"] == pytest.approx(breakeven_hours(D("12"), D("14"), D("113")), abs=1e-6)
    assert info["ok"] is False and info["reason_code"] == "breakeven_too_long"
    # the same numbers pass when steady income is real
    info = strategy.evaluate_entry_gate(opp("ATOM", next_hour="206", steady="196"), cfg(), tracker, D("0"), snap)
    assert info["ok"] is True


class _Book:
    def __init__(self, bids, asks):
        self._b = {"bids": bids, "asks": asks}

    def get_book(self, symbol, depth=5):
        return self._b

    def get_increments(self, symbol):
        return D("0.001"), D("0.01")


def test_market_snapshot_empty_book_raises_clean_error():
    aster = _Book([(D("1.74"), D("1"))], [(D("1.75"), D("1"))])
    hl = _Book([], [])
    with pytest.raises(DexAPIError, match="Hyperliquid book for AI is empty"):
        strategy.market_snapshot(aster, hl, "AI")
