# src/sources/yahoo_source.py
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import requests

try:
    import yfinance as yf
except ImportError:
    yf = None


def _ensure_yf():
    if yf is None:
        raise RuntimeError("yfinance no está instalado. pip install yfinance")


def _now_iso_utc() -> str:
    return datetime.now(UTC).isoformat()


def _last_close_any(t) -> float | None:
    # intenta intradía; si falla, usa diario de 5 días
    data = t.history(period="1d", interval="1m")
    if data is not None and not data.empty:
        s = data["Close"].dropna()
        if not s.empty:
            return float(s.iloc[-1])
    data = t.history(period="5d", interval="1d")
    if data is not None and not data.empty:
        s = data["Close"].dropna()
        if not s.empty:
            return float(s.iloc[-1])
    return None


def get_vix_quote() -> dict[str, Any] | None:
    _ensure_yf()
    sym = "^VIX"
    t = yf.Ticker(sym)
    info = getattr(t, "fast_info", None)
    if info and getattr(info, "last_price", None) is not None:
        value = float(info.last_price)
    else:
        value = _last_close_any(t)
        if value is None:
            return None
    return {"symbol": sym, "value": value, "time": _now_iso_utc()}


def get_dxy_quote(symbols: list[str] | None = None) -> dict[str, Any] | None:
    """
    Intenta DXY probando primero los símbolos provistos, luego fallback.
    Normaliza 'DX-Y..NYB' -> 'DX-Y.NYB'.
    """
    _ensure_yf()
    # normaliza candidatos del YAML/llamada
    norm = []
    if symbols:
        for s in symbols:
            if not s:
                continue
            s2 = s.replace("..", ".").strip()
            norm.append(s2)

    # fallback comunes
    fallback = ["DX-Y.NYB", "DX=F", "^DXY"]
    candidates = norm + [x for x in fallback if x not in norm]

    last_err = None
    for sym in candidates:
        try:
            t = yf.Ticker(sym)
            info = getattr(t, "fast_info", None)
            if info and getattr(info, "last_price", None) is not None:
                return {"symbol": sym, "value": float(info.last_price), "time": _now_iso_utc()}
            val = _last_close_any(t)
            if val is not None:
                return {"symbol": sym, "value": float(val), "time": _now_iso_utc()}
        except Exception as e:
            last_err = e
            continue
    return None


def get_ust10y_from_yahoo(symbol: str = "^TNX") -> dict[str, Any] | None:
    """
    ^TNX (CBOE 10Y). Históricamente: valor ~ yield*10 (41.5 => 4.15%).
    Pero algunos proveedores pueden entregar directamente % (4.15) o incluso decimal.
    Heurística:
      - si raw >= 20  -> % = raw/10
      - elif 2 <= raw < 20 -> % = raw
      - elif raw < 2  -> asumimos decimal (0.0415) y convertimos a % = raw*100
    Devuelve 'value_pct' y 'value_dec' (decimal).
    """
    _ensure_yf()
    t = yf.Ticker(symbol)
    raw = None
    info = getattr(t, "fast_info", None)
    if info and getattr(info, "last_price", None) is not None:
        raw = float(info.last_price)
    else:
        val = _last_close_any(t)
        if val is not None:
            raw = float(val)
    if raw is None:
        return None

    if raw >= 20:
        pct = raw / 10.0
    elif raw >= 2:
        pct = raw
    else:
        pct = raw * 100.0

    dec = pct / 100.0
    return {"symbol": symbol, "value_pct": pct, "value_dec": dec, "time": _now_iso_utc()}


# --- NUEVO helper genérico ---
def get_yahoo_index_quote(symbol: str) -> dict[str, Any] | None:
    _ensure_yf()
    t = yf.Ticker(symbol)
    info = getattr(t, "fast_info", None)
    if info and getattr(info, "last_price", None) is not None:
        v = float(info.last_price)
    else:
        v = _last_close_any(t)
        if v is None:
            return None
    return {"symbol": symbol, "value": float(v), "time": _now_iso_utc()}


# --- NUEVOS wrappers (simples, por claridad) ---
def get_move_quote() -> dict[str, Any] | None:
    return get_yahoo_index_quote("^MOVE")


def get_gvz_quote() -> dict[str, Any] | None:
    return get_yahoo_index_quote("^GVZ")


def get_ovx_quote() -> dict[str, Any] | None:
    return get_yahoo_index_quote("^OVX")


def get_vxn_quote() -> dict[str, Any] | None:
    return get_yahoo_index_quote("^VXN")


def get_skew_quote() -> dict[str, Any] | None:
    return get_yahoo_index_quote("^SKEW")


# --- Genérico para precio Yahoo (ETF/Futuros/Índices) ---
def get_yahoo_price(symbol: str) -> dict[str, Any] | None:
    _ensure_yf()
    t = yf.Ticker(symbol)
    info = getattr(t, "fast_info", None)
    if info and getattr(info, "last_price", None) is not None:
        v = float(info.last_price)
    else:
        v = _last_close_any(t)
        if v is None:
            return None
    return {"symbol": symbol, "value": float(v), "time": _now_iso_utc()}


# Wrappers específicos (claridad)
def get_copper_quote() -> dict[str, Any] | None:
    return get_yahoo_price("HG=F")


def get_gold_fut_quote() -> dict[str, Any] | None:
    return get_yahoo_price("GC=F")


def get_hyg_quote() -> dict[str, Any] | None:
    return get_yahoo_price("HYG")


def get_lqd_quote() -> dict[str, Any] | None:
    return get_yahoo_price("LQD")


def _read_spx_constits(path: str = "configs/spx_constituents.csv") -> list[str]:
    p = Path(path)
    if not p.exists():
        return []
    df = pd.read_csv(p)
    syms = [str(s).strip() for s in df["symbol"].dropna().tolist() if str(s).strip()]
    return syms


def get_breadth_spx_pct_above(
    ma_days: int, path: str = "configs/spx_constituents.csv"
) -> dict[str, Any] | None:
    _ensure_yf()
    syms = _read_spx_constits(path)
    if not syms:
        return None
    # bajamos 260 días aprox. para 200d MA
    data = yf.download(
        syms, period="300d", interval="1d", group_by="ticker", auto_adjust=False, progress=False
    )
    if data is None or len(data) == 0:
        return None

    hits = 0
    tot = 0
    for s in syms:
        try:
            px = data[(s, "Close")] if (s, "Close") in data.columns else data["Close"][s]
            px = px.dropna()
            if len(px) < ma_days + 1:
                continue
            ma = px.rolling(ma_days).mean()
            if np.isnan(ma.iloc[-1]):
                continue
            last = float(px.iloc[-1])
            above = last > float(ma.iloc[-1])
            hits += int(above)
            tot += 1
        except Exception:
            continue

    if tot == 0:
        return None
    pct = 100.0 * hits / tot
    return {
        "symbol": f"SPX_ABOVE_{ma_days}D",
        "value": pct,
        "time": _now_iso_utc(),
        "total": tot,
        "hits": hits,
    }


def get_breadth_spx_adv_dec(path: str = "configs/spx_constituents.csv") -> dict[str, Any] | None:
    _ensure_yf()
    syms = _read_spx_constits(path)
    if not syms:
        return None
    # día actual y previo
    data = yf.download(
        syms, period="5d", interval="1d", group_by="ticker", auto_adjust=False, progress=False
    )
    if data is None or len(data) == 0:
        return None

    adv = 0
    dec = 0
    unch = 0
    tot = 0
    for s in syms:
        try:
            px = data[(s, "Close")] if (s, "Close") in data.columns else data["Close"][s]
            px = px.dropna()
            if len(px) < 2:
                continue
            chg = float(px.iloc[-1]) - float(px.iloc[-2])
            if chg > 0:
                adv += 1
            elif chg < 0:
                dec += 1
            else:
                unch += 1
            tot += 1
        except Exception:
            continue

    if tot == 0:
        return None
    net = adv - dec
    return {
        "symbol": "SPX_ADV_DEC",
        "value": float(net),
        "time": _now_iso_utc(),
        "total": tot,
        "adv": adv,
        "dec": dec,
        "unch": unch,
    }


def get_rel_volume_20d(symbol: str) -> dict[str, Any] | None:
    _ensure_yf()
    t = yf.Ticker(symbol)
    # 21 días para promedio ~20 sesiones
    hist = t.history(period="30d", interval="1d")
    if hist is None or hist.empty or "Volume" not in hist.columns:
        return None
    vol = float(hist["Volume"].dropna().iloc[-1])
    avg20 = (
        float(hist["Volume"].dropna().tail(20).mean())
        if len(hist["Volume"].dropna()) >= 20
        else None
    )
    if not avg20 or avg20 == 0:
        return None
    rel = vol / avg20
    return {"symbol": symbol, "value": rel, "time": _now_iso_utc(), "vol": vol, "avg20": avg20}


def _period_to_unix(date_str: str) -> int:
    import datetime as dt

    y, m, d = map(int, date_str.split("-"))
    return int(dt.datetime(y, m, d, 0, 0).timestamp())


def hist_close(symbol: str, start: str, end: str) -> list[dict] | None:
    """
    OHLCV histórico diario → [{'date':'YYYY-MM-DD','close':float}]
    Usa el endpoint de Yahoo CSV para consistencia.
    """
    try:
        url = f"https://query1.finance.yahoo.com/v7/finance/download/{symbol}"
        params = {
            "period1": _period_to_unix(start),
            "period2": _period_to_unix(end) + 86400,  # inclusive
            "interval": "1d",
            "events": "history",
            "includeAdjustedClose": "true",
        }
        r = requests.get(url, params=params, timeout=20)
        r.raise_for_status()
        lines = r.text.strip().splitlines()
        if not lines or lines[0].lower().startswith("404"):
            return None
        out: list[dict] = []
        # CSV header: Date,Open,High,Low,Close,Adj Close,Volume
        for i, row in enumerate(lines):
            if i == 0:
                continue
            parts = row.split(",")
            if len(parts) < 6:
                continue
            ds, close_s = parts[0], parts[4]
            if ds in ("null", "") or close_s in ("null", "NaN", ""):
                continue
            try:
                cl = float(close_s)
            except Exception:
                continue
            out.append({"date": ds, "close": cl})
        return out
    except Exception:
        return None


def get_move_history(start: str, end: str) -> list[dict] | None:
    """
    Intento de historia MOVE por Yahoo (^MOVE).
    Si no existe para tu región/cuenta, devuelve None (lo marcará como proxy).
    """
    rows = hist_close("^MOVE", start, end)
    if not rows:
        return None
    return [{"date": r["date"], "value": float(r["close"])} for r in rows]


def get_etf_history_close(symbol: str, start: str, end: str) -> list[dict] | None:
    rows = hist_close(symbol, start, end)
    if not rows:
        return None
    return [{"date": r["date"], "value": float(r["close"])} for r in rows]
