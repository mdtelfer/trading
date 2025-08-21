from __future__ import annotations

import pandas as pd


def bull_trendline_break(df: pd.DataFrame, slope_eps: float = 5e-5) -> pd.Series:
    """Detección simple: usa dos últimos pivot_low confirmados para trazar TL.
    Marca True cuando close cruza por encima de la TL y la pendiente es > eps.
    (Implementación ligera; se puede refinar con almacenamiento de pivotes.)
    """
    lows = df["low"]
    piv = (lows.shift(2) == lows.rolling(5, center=True).min()).shift(2)
    piv_idx = df.index[piv.fillna(False)]
    brk = pd.Series(False, index=df.index)
    if len(piv_idx) < 2:
        return brk
    # recorrer pares consecutivos
    for i in range(1, len(piv_idx)):
        i1, i2 = piv_idx[i - 1], piv_idx[i]
        x1, y1 = df.index.get_loc(i1), lows.loc[i1]
        x2, y2 = df.index.get_loc(i2), lows.loc[i2]
        slope = (y2 - y1) / max(1, (x2 - x1))
        if slope < slope_eps:
            continue
    # a partir de i2 hacia adelante, chequear cruces
    for j in range(df.index.get_loc(i2), len(df)):
        x = j
        y_tl = y1 + slope * (x - x1)
        if df["close"].iat[j] > y_tl and df["close"].iat[j - 1] <= y_tl:
            brk.iat[j] = True
        break
    return brk
