# ──────────────────────────────────────────────────────────────────────────────
# src/pivots_boxes.py
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import pandas as pd


def pivot_high(df: pd.DataFrame, l: int = 2, r: int = 2) -> pd.Series:
    # Un HH se confirma r barras después → no repaint si usamos close r velas luego
    highs = df["high"]
    ph = highs.shift(l) == highs.rolling(l + 1 + r, center=True).max()
    return ph.shift(r).fillna(False)


def pivot_low(df: pd.DataFrame, l: int = 2, r: int = 2) -> pd.Series:
    lows = df["low"]
    pl = lows.shift(l) == lows.rolling(l + 1 + r, center=True).min()
    return pl.shift(r).fillna(False)


def build_boxes(df: pd.DataFrame, atr_series: pd.Series, k: float = 1.0) -> pd.DataFrame:
    """Devuelve DataFrame con columnas:
    - box_top, box_mid, box_bot, box_state: 'none'|'active'|'broken'|'retest_hold'
    - box_id (simple incremental)
    """
    out = pd.DataFrame(index=df.index)
    out[["box_top", "box_mid", "box_bot"]] = float("nan")
    out["box_state"] = "none"
    box_id = 0
    state = "none"
    top = mid = bot = float("nan")

    for i in range(len(df)):
        atr_i = atr_series.iat[i]
        if pd.isna(atr_i):
            out.iat[i, out.columns.get_loc("box_state")] = state
            continue
        width = atr_i * k

        # Crear soporte si pivot_low confirmado
        if pivot_low(df).iat[i]:
            price = df["low"].iat[i]
            bot = price
            top = price + width
            mid = (top + bot) / 2
            state = "active"
            box_id += 1
        # Actualizar estado si close rompe por arriba
        if state == "active" and not pd.isna(top):
            if df["close"].iat[i] > top and df["close"].iat[i - 1] <= top:
                state = "broken"
        # Retest-hold tras ruptura (para longs)
        if state == "broken" and not pd.isna(mid):
            # pullback que toca y cierra > mid
            if (df["low"].iat[i] <= top) and (df["close"].iat[i] > mid):
                state = "retest_hold"
        out.iat[i, out.columns.get_loc("box_top")] = top
        out.iat[i, out.columns.get_loc("box_mid")] = mid
        out.iat[i, out.columns.get_loc("box_bot")] = bot
        out.iat[i, out.columns.get_loc("box_state")] = state
        out.iat[i, out.columns.get_loc("box_id")] = box_id

    out["box_id"] = out["box_state"].ne("none").cumsum()
    return out
