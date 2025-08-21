# ──────────────────────────────────────────────────────────────────────────────
# src/sessions.py
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

from datetime import datetime, time

import pytz

GYE_TZ = pytz.timezone("America/Guayaquil")


LONDON_RANGE = (time(3, 0), time(6, 0))  # local
NY_RANGE = (time(8, 30), time(11, 30))


def in_range_local(dt: datetime, rng: tuple[time, time]) -> bool:
    t = dt.astimezone(GYE_TZ).time()
    return rng[0] <= t <= rng[1]


def session_tag(dt: datetime) -> str:
    if in_range_local(dt, LONDON_RANGE):
        return "LDN"
    if in_range_local(dt, NY_RANGE):
        return "NY"
    return "OTHER"
