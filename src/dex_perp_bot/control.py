"""Safety switch shared between the dashboard and the bot via logs/control.json.

Modes:
  run      - normal operation
  pause    - no new trades or rebalances; existing positions are held
  flatten  - close all positions and cancel all orders, then switch to ``pause``

The dashboard only writes this file; the bot reads it every CONTROL_POLL_SECONDS
and acts on it. This keeps the dashboard free of exchange credentials.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

logger = logging.getLogger(__name__)

CONTROL_PATH = Path("logs/control.json")
CONTROL_POLL_SECONDS = 30
VALID_MODES = ("run", "pause", "flatten")


def read_control(path: Path = CONTROL_PATH) -> Dict[str, Any]:
    """Return the control state; a missing or corrupt file means ``run``."""
    if not path.exists():
        return {"mode": "run"}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        logger.warning("Cannot read control file %s (%s); assuming run", path, exc)
        return {"mode": "run"}
    if data.get("mode") not in VALID_MODES:
        return {"mode": "run"}
    return data


def write_control(mode: str, *, note: str = "", by: str = "dashboard", path: Path = CONTROL_PATH) -> Dict[str, Any]:
    if mode not in VALID_MODES:
        raise ValueError(f"invalid mode {mode!r}")
    data = {
        "mode": mode,
        "requested_at": datetime.now(timezone.utc).isoformat(),
        "by": by,
        "note": note,
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)
    return data


def trading_allowed(path: Path = CONTROL_PATH) -> bool:
    return read_control(path).get("mode") == "run"
