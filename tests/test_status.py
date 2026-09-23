"""Round-trip P&L built from venue records (HL fills/funding, Aster income)."""

import pytest

from src.dex_perp_bot.status import _closed_trades

MIN = 60_000


def hl_fill(t, coin, direction, sz, px, closed_pnl, fee):
    return {"time": t, "coin": coin, "dir": direction, "sz": str(sz), "px": str(px), "closedPnl": str(closed_pnl), "fee": str(fee)}


def aster(t, symbol, kind, income):
    return {"time": t, "symbol": symbol, "incomeType": kind, "income": str(income)}


def test_round_trip_sums_both_venues_since_previous_exit():
    fills = [
        hl_fill(0, "CASHCAT", "Open Short", 100, 1.0, 0, 0.02),
        hl_fill(60 * MIN, "CASHCAT", "Close Short", 100, 1.01, -1.0, 0.02),
        hl_fill(120 * MIN, "CASHCAT", "Open Long", 100, 1.0, 0, 0.02),
        hl_fill(180 * MIN, "CASHCAT", "Close Long", 100, 1.02, 2.0, 0.02),
    ]
    funding = [{"time": 30 * MIN, "delta": {"coin": "CASHCAT", "usdc": "0.10"}}]
    income = [
        aster(0, "CASHCATUSDT", "COMMISSION", -0.05),
        aster(30 * MIN, "CASHCATUSDT", "FUNDING_FEE", 0.20),
        aster(61 * MIN, "CASHCATUSDT", "REALIZED_PNL", 0.8),
        aster(61 * MIN, "CASHCATUSDT", "COMMISSION", -0.05),
        aster(185 * MIN, "CASHCATUSDT", "REALIZED_PNL", -1.5),
    ]
    trips = _closed_trades(income, funding, fills)
    assert [t["closed_ms"] for t in trips] == [185 * MIN, 61 * MIN]  # newest first
    first = trips[1]
    assert first["venues"] == ["aster", "hyperliquid"]
    assert first["trading"] == pytest.approx(-0.2)
    assert first["fees"] == pytest.approx(-0.14)
    assert first["funding"] == pytest.approx(0.3)
    assert first["net"] == pytest.approx(-0.04)
    assert first["hours"] == pytest.approx(61 / 60)
    assert first["close_notional"] == pytest.approx(101.0)
    assert trips[0]["trading"] == pytest.approx(0.5)


def test_trickling_close_is_one_trip_and_lone_leg_is_flagged():
    fills = [
        hl_fill(0, "GRAM", "Open Short", 10, 1.0, 0, 0.01),
        hl_fill(40 * MIN, "GRAM", "Close Short", 10, 1.0, -0.5, 0.01),
        hl_fill(100 * MIN, "SKY", "Open Short", 10, 1.0, 0, 0.01),
        hl_fill(140 * MIN, "SKY", "Close Short", 10, 1.0, -0.3, 0.01),
    ]
    income = [aster(45 * MIN, "GRAMUSDT", "REALIZED_PNL", 0.1), aster(65 * MIN, "GRAMUSDT", "REALIZED_PNL", 0.6)]
    trips = {t["symbol"]: t for t in _closed_trades(income, [], fills)}
    assert trips["GRAM"]["trading"] == pytest.approx(0.2)
    assert trips["GRAM"]["closed_ms"] == 65 * MIN
    assert trips["SKY"]["venues"] == ["hyperliquid"]
