# ──────────────────────────────────────────────────────────────────────────────
# src/indicators.py
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import numpy as np
import pandas as pd


def ema(series: pd.Series, n: int) -> pd.Series:
    return series.ewm(span=n, adjust=False).mean()


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    high, low, close = df["high"], df["low"], df["close"].shift(1)
    tr = np.maximum(
        df["high"] - df["low"], np.maximum((df["high"] - close).abs(), (df["low"] - close).abs())
    )
    return tr.rolling(n).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    # intradía: pondremos volumen por tick como proxy
    pv = (df["close"] * df["tick_volume"]).cumsum()
    vv = (df["tick_volume"]).cumsum()
    return pv / vv


def rsi(series: pd.Series, n: int = 14) -> pd.Series:
    delta = series.diff()
    up = np.where(delta > 0, delta, 0.0)
    down = np.where(delta < 0, -delta, 0.0)
    roll_up = pd.Series(up, index=series.index).rolling(n).mean()
    roll_down = pd.Series(down, index=series.index).rolling(n).mean()
    rs = roll_up / (roll_down + 1e-12)
    return 100 - (100 / (1 + rs))


def roc(series: pd.Series, n: int = 12) -> pd.Series:
    return series.pct_change(n)


def atr_norm(df: pd.DataFrame, n: int = 14) -> pd.Series:
    a = atr(df, n)
    return a / df["close"].replace(0, np.nan)
