"""Structured decision log (JSONL) so every signal, gate and fill can be audited later.

One JSON object per line in logs/decisions.jsonl. ``kind`` values:
  scan         - hourly funding scan summary
  gate         - entry / switch decision with break-even and basis inputs
  leg_plan     - per-leg tactic chosen from imbalance / queue / flow
  leg_fill     - per-leg outcome: tactic used, wait, slippage vs decision mid
  pair_result  - both legs summary (hedged or not)
  basis_exit   - position closed to capture basis
  note         - free-form
"""

from __future__ import annotations

import json
import logging
import time
from collections import defaultdict
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path
from typing import Any, Dict, List

logger = logging.getLogger(__name__)

DECISIONS_PATH = Path("logs/decisions.jsonl")


def _clean(value: Any) -> Any:
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, float) and (value != value or value in (float("inf"), float("-inf"))):
        return None
    if isinstance(value, dict):
        return {k: _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    return value


def log_event(kind: str, path: Path = DECISIONS_PATH, **fields: Any) -> Dict[str, Any]:
    event = {"ts": datetime.now(timezone.utc).isoformat(), "kind": kind, **_clean(fields)}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, default=str) + "\n")
    except OSError as exc:
        logger.warning("Could not write decision log: %s", exc)
    logger.info("DECISION %s %s", kind, json.dumps({k: v for k, v in event.items() if k not in ("ts", "kind")}, default=str))
    return event


def read_events(path: Path = DECISIONS_PATH, limit: int = 200) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    events: List[Dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except ValueError:
                    continue
    except OSError:
        return []
    return events[-limit:][::-1]


def summarize(path: Path = DECISIONS_PATH) -> Dict[str, Any]:
    """Aggregate what worked: fill rate and slippage per tactic, gate outcomes, basis exits."""
    all_events = read_events(path, limit=100_000)
    tactics: Dict[str, Dict[str, Any]] = defaultdict(lambda: {"count": 0, "filled": 0, "crossed_after_wait": 0,
                                                              "slippage_bps_sum": 0.0, "wait_s_sum": 0.0})
    gates: Dict[str, int] = defaultdict(int)
    basis_exits = {"count": 0, "gain_bps_sum": 0.0}
    pairs = {"count": 0, "hedged": 0}
    for e in all_events:
        k = e.get("kind")
        if k == "leg_fill":
            t = tactics[e.get("planned_tactic", "?")]
            t["count"] += 1
            if e.get("filled"):
                t["filled"] += 1
            if e.get("final_tactic") == "cross" and e.get("planned_tactic") == "passive":
                t["crossed_after_wait"] += 1
            if e.get("slippage_bps") is not None:
                t["slippage_bps_sum"] += float(e["slippage_bps"])
            if e.get("wait_s") is not None:
                t["wait_s_sum"] += float(e["wait_s"])
        elif k == "gate":
            gates[f"{e.get('decision')}:{e.get('reason_code', '')}"] += 1
        elif k == "basis_exit":
            basis_exits["count"] += 1
            basis_exits["gain_bps_sum"] += float(e.get("gain_bps") or 0)
        elif k == "pair_result":
            pairs["count"] += 1
            if e.get("hedged"):
                pairs["hedged"] += 1
    out_tactics = {}
    for name, t in tactics.items():
        n = t["count"] or 1
        out_tactics[name] = {
            "count": t["count"], "fill_rate": t["filled"] / n,
            "crossed_after_wait": t["crossed_after_wait"],
            "avg_slippage_bps": t["slippage_bps_sum"] / n, "avg_wait_s": t["wait_s_sum"] / n,
        }
    return {"tactics": out_tactics, "gates": dict(gates), "basis_exits": basis_exits, "pairs": pairs,
            "events": len(all_events)}
