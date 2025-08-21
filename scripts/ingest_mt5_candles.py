# scripts/ingest_mt5_candles.py  (concurrente)
from concurrent.futures import ThreadPoolExecutor, as_completed
import datetime as dt
import os
import threading
import time

import MetaTrader5 as mt5
import numpy as np
import pandas as pd
from psycopg2.extras import execute_values
from psycopg2.pool import ThreadedConnectionPool
import pytz

# ----------------- Config -----------------
PG_DSN = os.getenv("PG_DSN", "dbname=trading host=localhost user=postgres password=postgres")
CSV_PATH = os.path.join("configs", "portfolio.csv")

df_symbols = pd.read_csv(CSV_PATH)
SYMBOLS = df_symbols["symbol"].dropna().unique().tolist()
print("Símbolos cargados:", SYMBOLS)

TF_MAP = {
    "M15": mt5.TIMEFRAME_M15,
    "H1": mt5.TIMEFRAME_H1,
    "H4": mt5.TIMEFRAME_H4,
    "D1": mt5.TIMEFRAME_D1,
}
# Prioridad: primero TF cortos para “ver vida” rápido
TFS_ORDER = ["M15", "H1", "H4", "D1"]

UTC = pytz.UTC
MT5_LOCK = threading.Lock()  # serializa llamadas al terminal MT5

# Rango por TF (ajústalo a gusto)
RANGES = {
    "M15": (dt.datetime(2023, 1, 1, tzinfo=UTC), dt.datetime.now(UTC)),
    "H1": (dt.datetime(2019, 1, 1, tzinfo=UTC), dt.datetime.now(UTC)),
    "H4": (dt.datetime(2019, 1, 1, tzinfo=UTC), dt.datetime.now(UTC)),
    "D1": (dt.datetime(2019, 1, 1, tzinfo=UTC), dt.datetime.now(UTC)),
}

MAX_WORKERS = int(os.getenv("INGEST_WORKERS", max(8, min(12, (os.cpu_count() or 4) * 2))))
POOL_MIN = int(os.getenv("PG_POOL_MIN", 2))
POOL_MAX = int(os.getenv("PG_POOL_MAX", max(POOL_MIN + 2, MAX_WORKERS + 2)))


# ----------------- Helpers -----------------
def mt5_copy_rates_range_safe(symbol, tf_code, start, end, attempts=3, base_sleep=0.5):
    """Llama a copy_rates_range con lock y reintentos suaves."""
    for i in range(attempts):
        with MT5_LOCK:
            rates = mt5.copy_rates_range(symbol, tf_code, start, end)
        if rates is not None and len(rates) > 0:
            return rates
        time.sleep(base_sleep * (2**i))
    return []


def fetch_rates(symbol, tf_str, date_from, date_to):
    tf_code = TF_MAP[tf_str]

    if date_from.tzinfo is None:
        date_from = UTC.localize(date_from)
    if date_to.tzinfo is None:
        date_to = UTC.localize(date_to)

    rates_chunks = []
    cur_from = date_from
    # Pedimos en ventanas de hasta 60 días para no sobrecargar
    while cur_from < date_to:
        cur_to = min(cur_from + dt.timedelta(days=60), date_to)
        res = mt5_copy_rates_range_safe(symbol, tf_code, cur_from, cur_to)
        if not res:
            break

        df = pd.DataFrame(res)
        # Epoch -> UTC aware
        df["ts_utc"] = pd.to_datetime(df["time"], unit="s", utc=True)

        if "spread" in df.columns:
            cols = ["ts_utc", "open", "high", "low", "close", "tick_volume", "spread"]
        else:
            cols = ["ts_utc", "open", "high", "low", "close", "tick_volume"]
            df["spread"] = np.nan
            cols.append("spread")

        df = df[cols]
        rates_chunks.append(df)

        cur_from = df["ts_utc"].max().to_pydatetime() + dt.timedelta(seconds=1)

    if rates_chunks:
        out = (
            pd.concat(rates_chunks, ignore_index=True)
            .drop_duplicates(subset=["ts_utc"])
            .sort_values("ts_utc")
        )
        return out

    return pd.DataFrame()


def upsert_candles(conn, symbol, tf_str, df):
    if df.empty:
        return 0

    rows = [
        (
            symbol,
            tf_str,
            r.ts_utc.to_pydatetime(),
            float(r.open),
            float(r.high),
            float(r.low),
            float(r.close),
            int(r.tick_volume) if not np.isnan(r.tick_volume) else None,
            float(r.spread) if not np.isnan(r.spread) else None,
        )
        for r in df.itertuples()
    ]

    with conn.cursor() as cur:
        # Ajustes de sesión para ingesta (opcionales, mejor rendimiento)
        cur.execute("SET application_name = 'ingest_mt5';")
        cur.execute("SET LOCAL synchronous_commit = off;")
        cur.execute("SET LOCAL lock_timeout = '2s';")
        cur.execute("SET LOCAL statement_timeout = '90s';")

        execute_values(
            cur,
            """
            INSERT INTO core.market_candles
              (symbol, tf, ts_utc, open, high, low, close, tick_volume, spread)
            VALUES %s
            ON CONFLICT (symbol, tf, ts_utc) DO UPDATE SET
              open = EXCLUDED.open,
              high = EXCLUDED.high,
              low = EXCLUDED.low,
              close = EXCLUDED.close,
              tick_volume = EXCLUDED.tick_volume,
              spread = EXCLUDED.spread
            """,
            rows,
            page_size=2000,
        )
    conn.commit()
    return len(rows)


def task(pair, pool):
    symbol, tf_str = pair
    d0, d1 = RANGES[tf_str]
    try:
        df = fetch_rates(symbol, tf_str, d0, d1)
        conn = pool.getconn()
        try:
            n = upsert_candles(conn, symbol, tf_str, df)
            return (symbol, tf_str, n, None)
        finally:
            pool.putconn(conn)
    except Exception as e:
        return (symbol, tf_str, 0, repr(e))


# ----------------- Main -----------------
def main_backfill():
    # MT5 se inicializa UNA sola vez
    assert mt5.initialize(), f"MT5 init failed: {mt5.last_error()}"

    # Pool de conexiones (thread-safe)
    pool = ThreadedConnectionPool(POOL_MIN, POOL_MAX, dsn=PG_DSN)

    # Lista de tareas (símbolo, tf) priorizando TFs cortos
    pairs = [(s, tf) for tf in TFS_ORDER for s in SYMBOLS]

    print(f"Workers: {MAX_WORKERS} | Pool: {POOL_MIN}-{POOL_MAX} | Tareas: {len(pairs)}")
    ok_total = 0
    err_total = 0

    try:
        with ThreadPoolExecutor(max_workers=MAX_WORKERS) as ex:
            futs = [ex.submit(task, p, pool) for p in pairs]
            for f in as_completed(futs):
                symbol, tf, n, err = f.result()
                if err is None:
                    ok_total += n
                    print(f"[OK] {symbol} {tf}: upserted {n}")
                else:
                    err_total += 1
                    print(f"[ERR] {symbol} {tf}: {err}")
    finally:
        try:
            pool.closeall()
        except Exception:
            pass
        mt5.shutdown()

    print(f"Done. Filas upsertadas: {ok_total} | Tareas con error: {err_total}")


if __name__ == "__main__":
    main_backfill()
