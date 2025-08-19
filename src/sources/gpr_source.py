# src/sources/gpr_source.py
from __future__ import annotations

import csv
from datetime import UTC, datetime
import io
from typing import Any

import requests

CANDIDATE_URLS = [
    # Host actual (autores)
    "https://www.matteoiacoviello.com/gpr_files/GPR_Global.csv",
    # Compatibilidad con el host viejo (a veces sigue sirviendo/copias)
    "https://www.policyuncertainty.com/media/GPR_Global.csv",
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Gecko/20100101 Firefox/124.0",
    "Accept": "text/csv,application/octet-stream;q=0.9,*/*;q=0.8",
    "Connection": "keep-alive",
}


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _parse_csv_bytes(b: bytes) -> dict[str, Any] | None:
    text = b.decode("utf-8", errors="ignore")

    # Algunos mirrors usan ';' como separador. Probamos ambos.
    for delim in [",", ";"]:
        reader = csv.reader(io.StringIO(text), delimiter=delim)
        rows = [r for r in reader if r and len(r) >= 2]
        if not rows:
            continue

        # intenta saltar encabezados no numéricos
        last = None
        for r in rows:
            try:
                float(r[1])
                last = r
            except Exception:
                continue

        if last is None:
            continue

        ym, val = last[0], float(last[1])
        return {"symbol": "GPR_Global", "value": val, "time": _now_iso(), "period": ym}
    return None


def get_gpr_global() -> dict[str, Any] | None:
    for url in CANDIDATE_URLS:
        try:
            r = requests.get(url, timeout=25, headers=HEADERS, allow_redirects=True)
            r.raise_for_status()
            parsed = _parse_csv_bytes(r.content)
            if parsed:
                parsed["via"] = url
                return parsed
        except Exception:
            continue
    return None
