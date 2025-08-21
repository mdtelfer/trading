# ──────────────────────────────────────────────────────────────────────────────
# src/types.py
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

SessionTag = Literal["LDN", "NY", "ASIA", "OTHER"]
SetupID = Literal["BRK_RT", "MR_DYN", "MOM_PB"]
Confidence = Literal["B", "A", "A+"]


class TechEvent(BaseModel):
    type: Literal["tech_event"] = "tech_event"
    symbol: str
    tf: Literal["M5", "M15", "H1", "H4", "D1"]
    event: str
    level: float
    session: SessionTag
    ts: str
    features: dict[str, float] = Field(default_factory=dict)
    structure: dict[str, Any] = Field(default_factory=dict)


class Signal(BaseModel):
    type: Literal["signal"] = "signal"
    setup_id: SetupID
    side: Literal["buy"] = "buy"  # long-only
    symbol: str
    entry: str | float
    sl: str | float
    tp1_rr: float
    confidence: Confidence
    risk_group: Literal["core", "extend", "greed"]
    risk_R: float
    session: SessionTag
    ts: str
    meta: dict[str, Any] = Field(default_factory=dict)
