"""Periodic status snapshot (balances, positions, P&L) written to logs/status.json for the dashboard."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import funding
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


def _aster_pnl(aster_client: AsterClient, now_ms: int) -> Dict[str, Dict[str, float]]:
    start = now_ms - PNL_WINDOWS_HOURS["30d"] * 3600 * 1000
    records = aster_client.get_income_history(start)
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


def _hl_pnl(hl_client: HyperliquidClient, now_ms: int) -> Dict[str, Dict[str, float]]:
    start = now_ms - PNL_WINDOWS_HOURS["30d"] * 3600 * 1000
    out = {w: _empty_pnl() for w in PNL_WINDOWS_HOURS}
    for rec in hl_client.get_funding_history(start):
        t = int(rec.get("time", 0))
        amount = _f((rec.get("delta") or {}).get("usdc"))
        for w, hours in PNL_WINDOWS_HOURS.items():
            if t >= now_ms - hours * 3600 * 1000:
                out[w]["funding"] += amount
    for fill in hl_client.get_fills(start):
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
        })
    return rows


def write_status(
    aster_client: AsterClient,
    hl_client: HyperliquidClient,
    *,
    path: Path = STATUS_PATH,
    next_window_utc: Optional[datetime] = None,
    started_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Collect a snapshot and write it as JSON. Errors are recorded in the snapshot, never raised."""
    now_ms = int(time.time() * 1000)
    snap: Dict[str, Any] = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "started_at": started_at.isoformat() if started_at else None,
        "next_window_utc": next_window_utc.isoformat() if next_window_utc else None,
        "balances": {},
        "positions": [],
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
        snap["pnl"]["hyperliquid"] = _hl_pnl(hl_client, now_ms)
    except Exception as exc:
        snap["errors"].append(f"hl pnl: {exc}")
    try:
        snap["pnl"]["aster"] = _aster_pnl(aster_client, now_ms)
    except Exception as exc:
        snap["errors"].append(f"aster pnl: {exc}")
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
            "net_apy": _f(o.apy_difference), "basis": o.apy_difference_basis,
            "imminent": o.funding_is_imminent, "actionable": o.is_actionable,
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
