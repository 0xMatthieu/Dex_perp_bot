"""Periodic status snapshot (balances, positions, P&L) written to logs/status.json for the dashboard."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from decimal import Decimal
from typing import Any, Dict, List, Optional

from . import funding
from .basis import BasisTracker
from .config import ExecutionConfig
from .control import read_control
from .exchanges.aster import AsterClient
from .exchanges.hyperliquid import HyperliquidClient

logger = logging.getLogger(__name__)

STATUS_PATH = Path("logs/status.json")
PNL_WINDOWS_HOURS = {"24h": 24, "7d": 24 * 7, "30d": 24 * 30}


def _f(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _empty_pnl() -> Dict[str, float]:
    return {"funding": 0.0, "trading": 0.0, "fees": 0.0, "net": 0.0}


def _aster_pnl(records: List[Dict[str, Any]], now_ms: int) -> Dict[str, Dict[str, float]]:
    out = {w: _empty_pnl() for w in PNL_WINDOWS_HOURS}
    for rec in records:
        t = int(rec.get("time", 0))
        amount = _f(rec.get("income"))
        kind = rec.get("incomeType", "")
        for w, hours in PNL_WINDOWS_HOURS.items():
            if t < now_ms - hours * 3600 * 1000:
                continue
            if kind == "FUNDING_FEE":
                out[w]["funding"] += amount
            elif kind == "REALIZED_PNL":
                out[w]["trading"] += amount
            elif kind == "COMMISSION":
                out[w]["fees"] += amount  # already negative
    for w in out:
        out[w]["net"] = out[w]["funding"] + out[w]["trading"] + out[w]["fees"]
    return out


def _hl_pnl(funding_recs: List[Dict[str, Any]], fills: List[Dict[str, Any]], now_ms: int) -> Dict[str, Dict[str, float]]:
    out = {w: _empty_pnl() for w in PNL_WINDOWS_HOURS}
    for rec in funding_recs:
        t = int(rec.get("time", 0))
        amount = _f((rec.get("delta") or {}).get("usdc"))
        for w, hours in PNL_WINDOWS_HOURS.items():
            if t >= now_ms - hours * 3600 * 1000:
                out[w]["funding"] += amount
    for fill in fills:
        t = int(fill.get("time", 0))
        for w, hours in PNL_WINDOWS_HOURS.items():
            if t >= now_ms - hours * 3600 * 1000:
                out[w]["trading"] += _f(fill.get("closedPnl"))
                out[w]["fees"] -= _f(fill.get("fee"))
    for w in out:
        out[w]["net"] = out[w]["funding"] + out[w]["trading"] + out[w]["fees"]
    return out


def _positions(aster_client: AsterClient, hl_client: HyperliquidClient) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    for pos in hl_client.get_all_positions():
        rows.append({
            "venue": "Hyperliquid",
            "symbol": str(pos.get("symbol", "")).split("/")[0],
            "side": pos.get("side"),
            "size": _f(pos.get("contracts")),
            "entry": _f(pos.get("entryPrice")),
            "mark": _f(pos.get("markPrice")),
            "leverage": _f(pos.get("leverage")),
            "unrealized_pnl": _f(pos.get("unrealizedPnl")),
            "liquidation": _f(pos.get("liquidationPrice")),
            "notional": abs(_f(pos.get("notional"))) or abs(_f(pos.get("contracts")) * _f(pos.get("entryPrice"))),
        })
    for pos in aster_client.get_all_positions():
        amt = _f(pos.get("positionAmt"))
        rows.append({
            "venue": "Aster",
            "symbol": str(pos.get("symbol", "")).replace("USDT", ""),
            "side": "long" if amt > 0 else "short",
            "size": abs(amt),
            "entry": _f(pos.get("entryPrice")),
            "mark": _f(pos.get("markPrice")),
            "leverage": _f(pos.get("leverage")),
            "unrealized_pnl": _f(pos.get("unrealizedProfit")),
            "liquidation": _f(pos.get("liquidationPrice")),
            "notional": abs(_f(pos.get("notional"))) or abs(amt * _f(pos.get("entryPrice"))),
        })
    return rows


def _open_orders(aster_client: AsterClient, hl_client: HyperliquidClient) -> List[Dict[str, Any]]:
    """Resting orders on both venues (the anchor while an entry/exit is being worked)."""
    rows: List[Dict[str, Any]] = []
    for o in hl_client.get_all_open_orders():
        post_only = bool(o.get("postOnly")) or str(o.get("timeInForce") or "").upper() == "ALO"
        rows.append({
            "venue": "Hyperliquid", "symbol": str(o.get("symbol", "")).split("/")[0],
            "side": str(o.get("side", "")).lower(), "price": _f(o.get("price")), "qty": _f(o.get("amount")),
            "filled": _f(o.get("filled")), "id": str(o.get("id", "")),
            "tif": "post-only" if post_only else str(o.get("timeInForce") or ""),
        })
    for o in aster_client.get_all_open_orders():
        rows.append({
            "venue": "Aster", "symbol": str(o.get("symbol", "")).replace("USDT", ""),
            "side": str(o.get("side", "")).lower(), "price": _f(o.get("price")), "qty": _f(o.get("origQty")),
            "filled": _f(o.get("executedQty")), "id": str(o.get("orderId", "")),
            "tif": "post-only" if str(o.get("timeInForce") or "") == "GTX" else str(o.get("timeInForce") or ""),
        })
    return rows


def _iso_to_ms(value: Any) -> Optional[int]:
    try:
        dt = datetime.fromisoformat(str(value))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return int(dt.timestamp() * 1000)
    except (TypeError, ValueError):
        return None


def _trade(
    aster_client: AsterClient,
    hl_client: HyperliquidClient,
    positions: List[Dict[str, Any]],
    aster_income: List[Dict[str, Any]],
    hl_funding: List[Dict[str, Any]],
    hl_fills: List[Dict[str, Any]],
    now_ms: int,
    exec_cfg: Optional[ExecutionConfig],
    tracker: Optional[BasisTracker],
) -> Optional[Dict[str, Any]]:
    """The delta-neutral pair as one trade: what we hold, the basis since entry, the exit gate, and the
    funding it has earned. None when there is no position state (nothing open)."""
    from .microstructure import basis_gain_bps, favorable_z, funding_bps_per_hour
    from .strategy import hedge_cross_bps, load_position_state, market_snapshot

    state = load_position_state()
    if not state or not state.get("symbol"):
        return None
    symbol = str(state["symbol"])
    long_venue = str(state.get("long_venue") or "")
    entered_ms = _iso_to_ms(state.get("entered_at"))
    # Fills and fees of the entry itself land before ``entered_at`` (stamped once execution is done).
    since_ms = _iso_to_ms(state.get("entry_started_at")) or entered_ms
    net_apy = _f(state.get("net_apy_pct"))
    out: Dict[str, Any] = {
        "symbol": symbol, "long_venue": long_venue, "short_venue": state.get("short_venue"),
        "entered_at": state.get("entered_at"),
        "hold_hours": (now_ms - entered_ms) / 3.6e6 if entered_ms else None,
        "net_apy_pct": net_apy, "verified": bool(state.get("verified", True)),
        "entry_basis_bps": state.get("entry_basis_bps"), "entry_basis_note": state.get("entry_basis_note"),
        "legs": state.get("legs") or [],
        "basis_now_bps": None, "basis_gain_bps": None, "favorable_z": None, "basis_samples": None,
        "exit_threshold_bps": None, "z_exit": exec_cfg.z_exit if exec_cfg else None, "exit_ready": False,
        "errors": [],
    }
    mine = [p for p in positions if p.get("symbol") == symbol]
    out["positions"] = mine
    out["unrealized_pnl"] = sum(_f(p.get("unrealized_pnl")) for p in mine)
    out["notional_usd"] = sum(_f(p.get("notional")) for p in mine) / 2 if mine else 0.0  # per side
    out["funding_per_hour_usd"] = out["notional_usd"] * float(funding_bps_per_hour(Decimal(str(net_apy)))) / 10_000

    # Basis since entry and the exit gate (same arithmetic as strategy.check_basis_exit).
    try:
        snap = market_snapshot(aster_client, hl_client, symbol)
        now_bps = snap["basis_bps"]
        out["basis_now_bps"] = float(now_bps)
        out["half_spread_aster_bps"] = float(snap["half_spread_aster_bps"])
        out["half_spread_hl_bps"] = float(snap["half_spread_hl_bps"])
        entry = state.get("entry_basis_bps")
        gain = basis_gain_bps(Decimal(str(entry)), now_bps, long_venue) if entry is not None else None
        out["basis_gain_bps"] = float(gain) if gain is not None else None
        fz = None
        if tracker is not None and exec_cfg is not None:
            stats = tracker.stats(symbol, now_bps, exec_cfg.basis_window_hours, exec_cfg.basis_min_samples)
            fz = favorable_z(stats, long_venue)
            out["favorable_z"] = float(fz) if fz is not None else None
            out["basis_samples"] = stats.n
            out["basis_mean_bps"] = float(stats.mean_bps) if stats.mean_bps is not None else None
        if exec_cfg is not None:
            forgone = funding_bps_per_hour(Decimal(str(net_apy))) * Decimal(str(exec_cfg.expected_hold_hours))
            threshold = Decimal(str(exec_cfg.exit_cost_bps + exec_cfg.basis_exit_min_gain_bps)) + hedge_cross_bps(snap) + forgone
            out["exit_threshold_bps"] = float(threshold)
            out["exit_ready"] = bool(gain is not None and gain >= threshold and fz is not None and fz <= -Decimal(str(exec_cfg.z_exit)))
    except Exception as exc:
        out["errors"].append(f"basis: {exc}")

    # Funding and fees this trade has earned/paid since entry, per venue.
    funding_usd = {"hyperliquid": 0.0, "aster": 0.0}
    fees_usd = {"hyperliquid": 0.0, "aster": 0.0}
    if since_ms:
        for rec in hl_funding:
            d = rec.get("delta") or {}
            if int(rec.get("time", 0)) >= since_ms and str(d.get("coin", "")) == symbol:
                funding_usd["hyperliquid"] += _f(d.get("usdc"))
        for fill in hl_fills:
            if int(fill.get("time", 0)) >= since_ms and str(fill.get("coin", "")) == symbol:
                fees_usd["hyperliquid"] -= _f(fill.get("fee"))
        for rec in aster_income:
            if int(rec.get("time", 0)) < since_ms or str(rec.get("symbol", "")) != f"{symbol}USDT":
                continue
            if rec.get("incomeType") == "FUNDING_FEE":
                funding_usd["aster"] += _f(rec.get("income"))
            elif rec.get("incomeType") == "COMMISSION":
                fees_usd["aster"] += _f(rec.get("income"))
    funding_usd["total"] = funding_usd["hyperliquid"] + funding_usd["aster"]
    fees_usd["total"] = fees_usd["hyperliquid"] + fees_usd["aster"]
    out["funding_since_entry"] = funding_usd
    out["fees_since_entry"] = fees_usd
    return out


def write_status(
    aster_client: AsterClient,
    hl_client: HyperliquidClient,
    *,
    path: Path = STATUS_PATH,
    next_window_utc: Optional[datetime] = None,
    started_at: Optional[datetime] = None,
    exec_cfg: Optional[ExecutionConfig] = None,
    tracker: Optional[BasisTracker] = None,
) -> Dict[str, Any]:
    """Collect a snapshot and write it as JSON. Errors are recorded in the snapshot, never raised."""
    now_ms = int(time.time() * 1000)
    history_start = now_ms - PNL_WINDOWS_HOURS["30d"] * 3600 * 1000
    snap: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at.isoformat() if started_at else None,
        "next_window_utc": next_window_utc.isoformat() if next_window_utc else None,
        "balances": {},
        "positions": [],
        "open_orders": [],
        "trade": None,
        "unrealized_pnl": 0.0,
        "pnl": {},
        "opportunities": [],
        "errors": [],
        "control": read_control(),
    }
    try:
        hl_bal = hl_client.get_wallet_balance()
        snap["balances"]["hyperliquid"] = {"total": _f(hl_bal.total), "available": _f(hl_bal.available)}
    except Exception as exc:
        snap["errors"].append(f"hl balance: {exc}")
    try:
        a_bal = aster_client.get_wallet_balance()
        snap["balances"]["aster"] = {"total": _f(a_bal.total), "available": _f(a_bal.available)}
    except Exception as exc:
        snap["errors"].append(f"aster balance: {exc}")
    try:
        snap["positions"] = _positions(aster_client, hl_client)
        snap["unrealized_pnl"] = sum(p["unrealized_pnl"] for p in snap["positions"])
    except Exception as exc:
        snap["errors"].append(f"positions: {exc}")
    try:
        snap["open_orders"] = _open_orders(aster_client, hl_client)
    except Exception as exc:
        snap["errors"].append(f"open orders: {exc}")
    hl_funding: List[Dict[str, Any]] = []
    hl_fills: List[Dict[str, Any]] = []
    aster_income: List[Dict[str, Any]] = []
    try:
        hl_funding = hl_client.get_funding_history(history_start)
        hl_fills = hl_client.get_fills(history_start)
        snap["pnl"]["hyperliquid"] = _hl_pnl(hl_funding, hl_fills, now_ms)
    except Exception as exc:
        snap["errors"].append(f"hl pnl: {exc}")
    try:
        aster_income = aster_client.get_income_history(history_start)
        snap["pnl"]["aster"] = _aster_pnl(aster_income, now_ms)
    except Exception as exc:
        snap["errors"].append(f"aster pnl: {exc}")
    try:
        snap["trade"] = _trade(aster_client, hl_client, snap["positions"], aster_income, hl_funding, hl_fills,
                               now_ms, exec_cfg, tracker)
    except Exception as exc:
        snap["errors"].append(f"trade: {exc}")
    if snap["pnl"]:
        total: Dict[str, Dict[str, float]] = {}
        for w in PNL_WINDOWS_HOURS:
            total[w] = _empty_pnl()
            for venue in snap["pnl"].values():
                for k in total[w]:
                    total[w][k] += venue.get(w, {}).get(k, 0.0)
        snap["pnl"]["total"] = total
    snap["opportunities"] = [
        {
            "symbol": o.symbol, "long_venue": o.long_venue, "short_venue": o.short_venue,
            "net_apy": _f(o.apy_difference), "apy_next_hour": _f(o.apy_next_hour), "apy_steady": _f(o.apy_steady),
            "basis": o.apy_difference_basis,
            "imminent": o.funding_is_imminent, "actionable": o.is_actionable,
            "gate": funding.LAST_GATE_REASONS.get(o.symbol),
            "rate_aster": _f(o.rate_aster), "rate_hyperliquid": _f(o.rate_hyperliquid),
        }
        for o in funding.LAST_OPPORTUNITIES
    ]
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(snap, indent=2, default=str), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        logger.error("Failed to write status file: %s", exc)
    return snap
