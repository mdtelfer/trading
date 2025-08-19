# src/sources/cnn_fng_source.py
from __future__ import annotations

from datetime import UTC, datetime
import json
import os
from pathlib import Path
import re
import time
from typing import Any

from bs4 import BeautifulSoup
import requests

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://edition.cnn.com/",
    "Connection": "keep-alive",
}

# La versión “edition” suele ser más estable
FNG_URLS = [
    "https://edition.cnn.com/markets/fear-and-greed",
    "https://money.cnn.com/data/fear-and-greed/",
]


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_from_html(html: str) -> int | None:
    """
    Extrae el valor 0..100 del índice.
    Estrategia:
      1) Buscar números 0..100 en spans/divs donde aparezca "fear"/"greed" cerca.
      2) Buscar en scripts JSON-like claves como fearGreedNow.
    """
    soup = BeautifulSoup(html, "html.parser")

    # 1) Spans/Divs visibles
    candidates: list[int] = []
    for el in soup.find_all(["span", "div"]):
        txt = (el.get_text(" ", strip=True) or "").replace(",", ".")
        if not txt:
            continue
        m = re.search(r"\b(\d{1,3})\b", txt)
        if not m:
            continue
        try:
            val = int(m.group(1))
        except Exception:
            continue
        if 0 <= val <= 100:
            around = txt.lower()
            if "fear" in around or "greed" in around:
                candidates.append(val)

    if candidates:
        # suele haber un número "destacado"; tomar el mayor es un heurístico seguro
        return max(candidates)

    # 2) Scripts embebidos (JSON-like)
    pat = re.compile(r'"(?:fearGreedNow|fear_greed_now)"\s*:\s*(\d{1,3})', re.IGNORECASE)
    for sc in soup.find_all("script"):
        blob = sc.string or sc.text or ""
        m = pat.search(blob)
        if not m:
            continue
        try:
            val = int(m.group(1))
        except Exception:
            continue
        if 0 <= val <= 100:
            return val

    return None


def fetch_fng() -> dict[str, Any] | None:
    last_err = None
    for url in FNG_URLS:
        try:
            r = requests.get(url, timeout=25, headers=HEADERS)
            r.raise_for_status()
            val = _parse_from_html(r.text)
            if val is not None:
                return {
                    "symbol": "CNN_FNG_STOCKS",
                    "value": float(val),
                    "time": _now_iso(),
                    "source": url,
                }
        except Exception as e:
            last_err = e
            time.sleep(0.8)
            continue

    # Fallback manual
    if os.getenv("FNG_STOCKS_ALLOW_MANUAL", "1").lower() in ("1", "true", "yes"):
        p = Path("configs") / "fng_stocks_manual.json"
        if p.exists():
            try:
                js = json.loads(p.read_text(encoding="utf-8"))
                val = int(js["value"])
                if 0 <= val <= 100:
                    return {
                        "symbol": "CNN_FNG_STOCKS",
                        "value": float(val),
                        "time": _now_iso(),
                        "source": "manual",
                    }
            except Exception:
                pass

    # No relanzamos: devolvemos None y el poller lo registra con warning
    return None
