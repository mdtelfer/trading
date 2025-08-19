# src/sources/fred_source.py
from __future__ import annotations

from datetime import UTC, datetime
import os
from typing import Any

import requests

FRED_API = "https://api.stlouisfed.org/fred/series/observations"


def _now_iso_utc() -> str:
    return datetime.now(UTC).isoformat()


def _fred_key() -> str | None:
    # lee de entorno
    return os.getenv("FRED_API_KEY")


def get_ust10y_fred(series: str = "DGS10") -> dict[str, Any] | None:
    api_key = os.getenv("FRED_API_KEY")
    if not api_key or api_key.strip().lower() in ("tu_clave", "your_key", "xxx"):
        # sin key válida, salimos en None para habilitar fallback
        return None

    params = {
        "series_id": series,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
        "observation_start": "2000-01-01",
    }
    try:
        r = requests.get(FRED_API, params=params, timeout=15)
        r.raise_for_status()
    except requests.HTTPError:
        # 400/403/429 -> dejar que el caller haga fallback
        return None

    js = r.json()
    obs = js.get("observations") or []
    if not obs:
        return None

    last = obs[0]
    val = last.get("value")
    if not val or val == "NaN":
        return None
    value = float(val)  # en %
    return {"symbol": series, "value": value, "time": _now_iso_utc(), "date": last.get("date")}


def get_oas_from_fred(series: str) -> dict[str, Any] | None:
    api_key = os.getenv("FRED_API_KEY")
    if not api_key:
        return None
    params = {
        "series_id": series,
        "api_key": api_key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
    }
    try:
        r = requests.get(FRED_API, params=params, timeout=15)
        r.raise_for_status()
        js = r.json()
        obs = js.get("observations") or []
        if not obs:
            return None
        last = obs[0]
        val = last.get("value")
        if not val or val == "NaN":
            return None
        return {
            "symbol": series,
            "value": float(val),
            "time": _now_iso_utc(),
            "date": last.get("date"),
        }
    except Exception:
        return None


def get_series_latest(series_id: str, api_key: str | None = None) -> float | None:
    """
    Devuelve el último valor numérico de una serie FRED como float (o None si no hay).
    Ej: get_series_latest("DFEDTARU") -> 5.5
    """
    key = api_key or _fred_key()
    if not key:
        return None

    params = {
        "series_id": series_id,
        "api_key": key,
        "file_type": "json",
        "sort_order": "desc",
        "limit": 1,
        "observation_start": "2000-01-01",
    }
    r = requests.get(FRED_API, params=params, timeout=20)
    r.raise_for_status()
    js = r.json()
    obs = js.get("observations") or []
    if not obs:
        return None
    last = obs[0]
    val = last.get("value")
    # FRED puede devolver "NaN", ".", ""
    if not val or val in ("NaN", ".", "null"):
        return None
    try:
        return float(val)
    except Exception:
        return None


def get_series_range(series_id: str, api_key: str, start: str, end: str) -> list[dict] | None:
    """
    Historia diaria FRED → [{'date':'YYYY-MM-DD','value':float}]
    """
    try:
        key = api_key or _fred_key()
        if not key:
            return None

        r = requests.get(
            FRED_API,
            params={
                "series_id": series_id,
                "api_key": key,
                "file_type": "json",
                "observation_start": start,
                "observation_end": end,
            },
            timeout=20,
        )
        r.raise_for_status()
        obs = r.json().get("observations", [])
        out: list[dict] = []
        for o in obs:
            ds = o.get("date")
            v = o.get("value")
            if ds is None or v in (None, "."):
                continue
            try:
                val = float(v)
            except Exception:
                continue
            out.append({"date": ds, "value": val})
        return out
    except Exception:
        return None
