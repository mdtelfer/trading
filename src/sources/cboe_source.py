# src/sources/cboe_source.py
from __future__ import annotations
from typing import Optional, Dict, Any
from datetime import datetime, timezone
import csv, io, time
import requests
from bs4 import BeautifulSoup

CSV_URL = "https://cdn.cboe.com/api/global/delayed_quotes/option_ratios.csv"
HTML_URL = "https://www.cboe.com/us/options/market_statistics/daily/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:124.0) Gecko/20100101 Firefox/124.0",
    "Accept": "text/csv,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Referer": "https://www.cboe.com/",
    "Connection": "keep-alive",
}

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

def _from_csv() -> Optional[Dict[str, Any]]:
    r = requests.get(CSV_URL, timeout=20, headers=HEADERS)
    r.raise_for_status()
    content = r.content.decode("utf-8", errors="ignore")
    reader = csv.DictReader(io.StringIO(content))
    last = None
    for row in reader:
        last = row
    if not last:
        return None
    # posibles claves: 'total','Total','Equity','Index'
    val = last.get("total") or last.get("Total")
    if val is None:
        # algunos dumps tienen 'All' para total
        val = last.get("All") or last.get("all")
    if val is None:
        return None
    return {
        "symbol": "CBOE_PCR_TOTAL",
        "value": float(val),
        "time": _now_iso(),
        "row": last,
        "source": "csv",
    }

def _from_html() -> Optional[Dict[str, Any]]:
    r = requests.get(HTML_URL, timeout=20, headers=HEADERS)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "html.parser")
    # La página tiene varias tablas; buscamos la sección "Put/Call Ratios"
    # Estructura típica: table con cabeceras ["All","Index","Equity"] y fila con ratios
    tables = soup.find_all("table")
    for tb in tables:
        ths = [th.get_text(strip=True).lower() for th in tb.find_all("th")]
        if not ths: 
            continue
        if "all" in ths and ("put/call" in " ".join(ths).lower() or "ratio" in " ".join(ths).lower()):
            # intenta leer la primera fila numérica
            trs = tb.find_all("tr")
            for tr in trs[1:]:
                tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                if not tds:
                    continue
                # heurística: primer valor suele ser 'All' ratio total
                try:
                    # busca columna "All"
                    if "all" in ths:
                        idx = ths.index("all")
                        total_val = float(tds[idx])
                        return {
                            "symbol": "CBOE_PCR_TOTAL",
                            "value": total_val,
                            "time": _now_iso(),
                            "row": dict(zip(ths, tds)),
                            "source": "html",
                        }
                except Exception:
                    continue
    return None

def get_total_pcr(retries: int = 2, backoff_sec: float = 0.8) -> Optional[Dict[str, Any]]:
    # 1) CSV con headers “reales” + reintentos
    for i in range(retries + 1):
        try:
            data = _from_csv()
            if data:
                return data
        except requests.HTTPError as e:
            # 403/429: intenta de nuevo tras backoff
            if e.response is not None and e.response.status_code in (403, 429):
                time.sleep(backoff_sec * (i + 1))
                continue
            return None
        except Exception:
            time.sleep(backoff_sec * (i + 1))
            continue

    # 2) Fallback HTML scrape
    try:
        data = _from_html()
        if data:
            return data
    except Exception:
        pass

    # 3) Nada disponible
    return None
