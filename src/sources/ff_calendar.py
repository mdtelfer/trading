# src/sources/ff_calendar.py
from __future__ import annotations
from typing import Optional, Dict, Any, List, Tuple
from datetime import datetime, timezone
from zoneinfo import ZoneInfo
import os, json, time
import requests
from pathlib import Path

# Endpoints alternativos (a veces falla DNS en el host cdn-*)
FF_URLS = [
    "https://cdn-nfs.faireconomy.media/ff_calendar_thisweek.json",
    "https://nfs.faireconomy.media/ff_calendar_thisweek.json",
]
HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Accept": "application/json,text/plain,*/*",
    "Connection": "keep-alive",
}

def _now_tz(tz: str) -> datetime:
    return datetime.now(ZoneInfo(tz))

def _to_dt(ts_ms: int, tz: str) -> datetime:
    return datetime.fromtimestamp(ts_ms / 1000, tz=ZoneInfo(tz))

def _imp_rank(impact: str) -> int:
    i = (impact or "").lower()
    if "high" in i or "alto" in i: return 3
    if "medium" in i or "med" in i: return 2
    if "low" in i or "bajo" in i or "baja" in i: return 1
    return 0

def _fetch_once(url: str, timeout: float = 15.0) -> Optional[List[Dict[str, Any]]]:
    r = requests.get(url, timeout=timeout, headers=HEADERS)
    r.raise_for_status()
    js = r.json()
    return js if isinstance(js, list) else None

def fetch_calendar(max_retries_per_url: int = 1, backoff_sec: float = 0.8) -> List[Dict[str, Any]]:
    """
    Intenta múltiples hosts. Si todos fallan, intenta fallback manual (configs/calendar_manual.json).
    """
    urls = [os.getenv("FF_CALENDAR_URL").strip()] if os.getenv("FF_CALENDAR_URL") else []
    urls += [u for u in FF_URLS if u not in urls]

    last_err = None
    for url in urls:
        for i in range(max_retries_per_url + 1):
            try:
                data = _fetch_once(url)
                if data: 
                    return data
            except Exception as e:
                last_err = e
                time.sleep(backoff_sec * (i + 1))
                continue

    # Fallback manual opcional
    manual = Path("configs") / "calendar_manual.json"
    if manual.exists():
        try:
            return json.loads(manual.read_text(encoding="utf-8"))
        except Exception:
            pass

    # Si nada funcionó, levanta la última excepción para que el caller decida
    if last_err:
        raise last_err
    return []

def compute_news_block(
    tz_local: str = "America/Guayaquil",
    lock_high_min: int = 15,
    lock_medium_min: int = 10,
) -> Dict[str, Any]:
    events = fetch_calendar()
    now_local = _now_tz(tz_local)

    candidates: List[Tuple[datetime, Dict[str, Any]]] = []
    for ev in events:
        try:
            ts_ms = int(ev.get("timestamp", 0))
            if ts_ms <= 0: 
                continue
            dt_local = _to_dt(ts_ms, tz_local)
            if dt_local >= now_local:
                candidates.append((dt_local, ev))
        except Exception:
            continue

    block = 0
    window_min_applied = 0
    next_ev: Optional[Dict[str, Any]] = None

    if candidates:
        candidates.sort(key=lambda x: x[0])
        ev_dt, ev = candidates[0]
        imp = _imp_rank(ev.get("impact"))

        win = 0
        if imp >= 3: win = lock_high_min
        elif imp == 2: win = lock_medium_min

        if win > 0:
            mins_to_event = (ev_dt - now_local).total_seconds() / 60.0
            if 0 <= mins_to_event <= win:
                block = 1
                window_min_applied = win

        next_ev = {
            "title": ev.get("title"),
            "impact": ev.get("impact"),
            "country": ev.get("country"),
            "timestamp_ms": ev.get("timestamp"),
            "when_local": ev_dt.isoformat(),
            "minutes_to_event": round((ev_dt - now_local).total_seconds()/60.0, 1)
        }

    return {
        "symbol": "FF_CALENDAR",
        "value": block,
        "time": datetime.now(timezone.utc).isoformat(),
        "next_event": next_ev,
        "window_min": window_min_applied,
        "tz": tz_local,
    }
