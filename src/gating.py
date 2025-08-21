# ──────────────────────────────────────────────────────────────────────────────
# src/gating.py
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

from datetime import datetime


def atr_norm_in_range(val: float, bounds: tuple[float, float]) -> bool:
    lo, hi = bounds
    return (val is not None) and (lo <= val <= hi)


def spread_ok(spread_pct: float, max_percentile_threshold: float) -> bool:
    return (spread_pct is not None) and (spread_pct <= max_percentile_threshold)


def news_block(now: datetime, events: list[tuple[datetime, datetime]]) -> bool:
    """True si estamos en ventana bloqueada por noticias de alto impacto."""
    return any(start <= now <= end for start, end in events)
