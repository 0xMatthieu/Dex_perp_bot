"""Rolling Aster-vs-Hyperliquid basis history per symbol, persisted to logs/basis_history.json."""

from __future__ import annotations

import json
import logging
import time
from collections import deque
from decimal import Decimal
from pathlib import Path
from typing import Deque, Dict, List, Optional, Tuple

from .microstructure import BasisStats, basis_bps, basis_stats

logger = logging.getLogger(__name__)

BASIS_HISTORY_PATH = Path("logs/basis_history.json")
Sample = Tuple[float, float]  # (ts_seconds, basis_bps as float)


class BasisTracker:
    """Keeps up to ``retention_hours`` of basis samples per symbol."""

    def __init__(self, path: Path = BASIS_HISTORY_PATH, retention_hours: float = 24.0) -> None:
        self._path = path
        self._retention_s = retention_hours * 3600
        self._samples: Dict[str, Deque[Sample]] = {}
        self._dirty = False
        self._load()

    # -- persistence -------------------------------------------------------
    def _load(self) -> None:
        if not self._path.exists():
            return
        try:
            raw = json.loads(self._path.read_text(encoding="utf-8"))
            for symbol, samples in raw.items():
                self._samples[symbol] = deque((float(t), float(b)) for t, b in samples)
            self._prune()
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("Could not load basis history %s: %s", self._path, exc)

    def save(self) -> None:
        if not self._dirty:
            return
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self._path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps({s: list(d) for s, d in self._samples.items()}), encoding="utf-8")
            tmp.replace(self._path)
            self._dirty = False
        except OSError as exc:
            logger.warning("Could not save basis history: %s", exc)

    def _prune(self) -> None:
        cutoff = time.time() - self._retention_s
        for symbol in list(self._samples):
            d = self._samples[symbol]
            while d and d[0][0] < cutoff:
                d.popleft()
            if not d:
                del self._samples[symbol]

    # -- sampling ------------------------------------------------------------
    def record(self, symbol: str, price_aster: Decimal, price_hl: Decimal, ts: Optional[float] = None) -> Decimal:
        b = basis_bps(price_aster, price_hl)
        self._samples.setdefault(symbol, deque()).append((ts or time.time(), float(b)))
        self._dirty = True
        return b

    def record_bps(self, symbol: str, value_bps: Decimal, ts: Optional[float] = None) -> None:
        self._samples.setdefault(symbol, deque()).append((ts or time.time(), float(value_bps)))
        self._dirty = True

    def samples(self, symbol: str, window_hours: float) -> List[Decimal]:
        cutoff = time.time() - window_hours * 3600
        return [Decimal(str(b)) for t, b in self._samples.get(symbol, ()) if t >= cutoff]

    def stats(self, symbol: str, current_bps: Decimal, window_hours: float, min_samples: int) -> BasisStats:
        return basis_stats(self.samples(symbol, window_hours), current_bps, min_samples=min_samples)

    def count(self, symbol: str) -> int:
        return len(self._samples.get(symbol, ()))

    def symbols(self) -> List[str]:
        return list(self._samples)
