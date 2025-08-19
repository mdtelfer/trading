from datetime import datetime

from .db import SessionLocal
from .models import FundamentalTick


def ingest_ticks(ticks: dict):
    """ticks = { feature: {symbol,bid,ask,time,...}, ... }"""
    with SessionLocal() as db:
        for feature, data in ticks.items():
            if not data:
                continue
            row = FundamentalTick(
                feature=feature,
                symbol=data["symbol"],
                bid=data["bid"],
                ask=data["ask"],
                time=datetime.fromisoformat(data["time"]),
                raw=data,
            )
            db.add(row)
        db.commit()
