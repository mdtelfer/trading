# ──────────────────────────────────────────────────────────────────────────────
# src/scoring.py
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

WEIGHTS_DEFAULT = {
    "regime_h1": 20,
    "level_confluence": 25,
    "session_timing": 10,
    "volatility_spread_ok": 15,
    "candle_structure": 20,
    "cleanliness": 10,
}


def score_confluence(
    features: dict[str, float | bool], weights: dict[str, int] = WEIGHTS_DEFAULT
) -> int:
    s = 0
    # Cada feature-flag suma si True (o si valor dentro de rango)
    s += weights["regime_h1"] if features.get("regime_h1_ok", False) else 0
    s += weights["level_confluence"] if features.get("level_ok", False) else 0
    s += weights["session_timing"] if features.get("session_ok", False) else 0
    s += weights["volatility_spread_ok"] if features.get("volatility_spread_ok", False) else 0
    s += weights["candle_structure"] if features.get("candle_ok", False) else 0
    s += weights["cleanliness"] if features.get("cleanliness_ok", False) else 0
    return int(s)


def bucket_confidence(score: int) -> str:
    if score >= 85:
        return "A+"
    if score >= 70:
        return "A"
    return "B"  # asume score ≥60
