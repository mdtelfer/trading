import os

import numpy as np
import pandas as pd
from psycopg2.extras import execute_values
from sqlalchemy import create_engine, text

PG_DSN = os.getenv("PG_DSN", "postgresql+psycopg2://postgres:postgres@localhost/trading")
engine = create_engine(PG_DSN)


def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    tr = pd.concat([(h - l), (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    return tr.rolling(n).mean()


def vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3.0
    vol = (
        df["tick_volume"].replace(0, np.nan).fillna(method="ffill")
    )  # proxy si no hay volumen real
    cum_pv = (tp * vol).cumsum()
    cum_v = vol.cumsum().replace(0, np.nan)
    out = cum_pv / cum_v
    return out


def bands(series, n=100):
    m = series.rolling(n).mean()
    s = series.rolling(n).std()
    return m, m - s, m + s


def compute_features(df):
    out = pd.DataFrame(index=df.index)
    out["ema20"] = ema(df["close"], 20)
    out["ema50"] = ema(df["close"], 50)
    out["ema200"] = ema(df["close"], 200)
    vw = vwap(df)
    out["vwap"] = vw
    mu, lo, hi = bands(vw, 100)
    out["vwap_sigma1"] = (vw - mu).abs() / (df["close"].rolling(100).std())
    out["vwap_sigma2"] = np.nan  # placeholder si no quieres 2σ real
    a = atr(df, 14)
    out["atr"] = a
    out["atr_norm"] = a / df["close"]
    delta = df["close"].pct_change()
    out["roc"] = df["close"] / df["close"].shift(12) - 1.0
    # RSI simple
    up = delta.clip(lower=0).rolling(14).mean()
    dn = (-delta.clip(upper=0)).rolling(14).mean()
    rs = up / (dn.replace(0, np.nan))
    out["rsi"] = 100 - (100 / (1 + rs))
    return out


def upsert_features(symbol, tf, df):
    # prepara filas
    rows = []
    for ts, r in df.iterrows():
        rows.append(
            (
                symbol,
                tf,
                ts.to_pydatetime(),
                _f(r.get("ema20")),
                _f(r.get("ema50")),
                _f(r.get("ema200")),
                _f(r.get("vwap")),
                _f(r.get("vwap_sigma1")),
                _f(r.get("vwap_sigma2")),
                _f(r.get("atr")),
                _f(r.get("atr_norm")),
                _f(r.get("rsi")),
                _f(r.get("roc")),
                "{}",
            )
        )
    with engine.begin() as conn:
        # asegurar particiones de cada mes
        months = sorted({pd.Timestamp(ts).strftime("%Y-%m-01") for ts in df.index})
        for m in months:
            conn.execute(text("SELECT core.ensure_month_partition('features', :m::date)"), {"m": m})
        # upsert masivo
        execute_values(
            conn.connection.cursor(),
            """
            INSERT INTO core.features(symbol, tf, ts_utc, ema20, ema50, ema200, vwap, vwap_sigma1, vwap_sigma2, atr, atr_norm, rsi, roc, extra)
            VALUES %s
            ON CONFLICT (symbol, tf, ts_utc) DO UPDATE SET
              ema20=EXCLUDED.ema20, ema50=EXCLUDED.ema50, ema200=EXCLUDED.ema200,
              vwap=EXCLUDED.vwap, vwap_sigma1=EXCLUDED.vwap_sigma1, vwap_sigma2=EXCLUDED.vwap_sigma2,
              atr=EXCLUDED.atr, atr_norm=EXCLUDED.atr_norm, rsi=EXCLUDED.rsi, roc=EXCLUDED.roc, extra=EXCLUDED.extra
        """,
            rows,
            page_size=2000,
        )


def _f(x):
    try:
        return float(x) if pd.notna(x) else None
    except:
        return None


def ingest_features_for(symbol, tf):
    with engine.begin() as conn:
        last_ts = conn.execute(
            text(
                """
            SELECT max(ts_utc) FROM core.features WHERE symbol=:s AND tf=:tf
        """
            ),
            {"s": symbol, "tf": tf},
        ).scalar()
        # carga candles desde último features_ts (o todo si None)
        q = """
          SELECT ts_utc, open, high, low, close, tick_volume
          FROM core.market_candles
          WHERE symbol=:s AND tf=:tf
          {cond}
          ORDER BY ts_utc
        """
        cond = "AND ts_utc > :ts" if last_ts else ""
        df = pd.read_sql(
            text(q.format(cond=cond)),
            conn,
            params={"s": symbol, "tf": tf, "ts": last_ts} if last_ts else {"s": symbol, "tf": tf},
        )
        if df.empty:
            return 0
        df["ts_utc"] = pd.to_datetime(df["ts_utc"], utc=True)
        df = df.set_index("ts_utc")
        feats = compute_features(df)
        feats = feats.dropna().copy()
        upsert_features(symbol, tf, feats)
        return len(feats)


if __name__ == "__main__":
    todo = [
        (sym, tf)
        for sym in [
            "EURUSD",
            "USDJPY",
            "GBPUSD",
            "XAUUSD",
            "SPX500",
            "NDX100",
            "US30",
            "USOIL",
            "BTCUSD",
            "ETHUSD",
        ]
        for tf in ["M15", "H1", "H4", "D1"]
    ]
    total = 0
    for sym, tf in todo:
        n = ingest_features_for(sym, tf)
        print(f"{sym} {tf}: features upserted {n}")
        total += n
    print("TOTAL rows:", total)
