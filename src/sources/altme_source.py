# src/sources/altme_source.py
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import requests

URL = "https://api.alternative.me/fng/?limit=1"


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def get_crypto_fng() -> dict[str, Any] | None:
    r = requests.get(URL, timeout=15)
    r.raise_for_status()
    js = r.json()
    data = js.get("data") or []
    if not data:
        return None
    d = data[0]
    # d: {"value":"71","value_classification":"Greed","timestamp":"1696800000",...}
    val = float(d.get("value"))
    ts = _now_iso()
    return {
        "symbol": "crypto_fng",
        "value": val,
        "time": ts,
        "label": d.get("value_classification"),
    }


def get_crypto_fng_history(start: str, end: str) -> list[dict] | None:
    """
    API soporta límite y formato timestamps. Aquí pedimos una ventana amplia
    y filtramos por fechas (start..end).
    """
    try:
        import datetime as dt

        # pedir suficiente: 2000 días ~ 5+ años
        r = requests.get("https://api.alternative.me/fng/", params={"limit": 2000}, timeout=15)
        r.raise_for_status()
        data = r.json().get("data", [])
        out = []
        for it in data:
            ts = int(it.get("timestamp", 0))
            ds = dt.datetime.utcfromtimestamp(ts).strftime("%Y-%m-%d")
            if ds < start or ds > end:
                continue
            val = float(it.get("value"))
            out.append({"date": ds, "value": val, "label": it.get("value_classification", "")})
        return sorted(out, key=lambda x: x["date"])
    except Exception:
        return None
