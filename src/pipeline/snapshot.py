# src/pipeline/snapshot.py
from __future__ import annotations
from typing import Dict, Any, List
import psycopg2
from psycopg2.extras import RealDictCursor
from src.config import env

NEEDED = ["VIX","DXY","UST10Y","USOIL","BRENT","XAUUSD","XAGUSD","BTCUSD","ETHUSD","CALENDAR_BLOCK"]

def pg_creds():
    return dict(
        host=env("DB_HOST","localhost"),
        port=int(env("DB_PORT","5432")),
        dbname=env("DB_NAME","trading"),
        user=env("DB_USER","postgres"),
        password=env("DB_PASSWORD","postgres"),
    )

def read_latest(features: List[str]) -> Dict[str, Any]:
    sql = """
    SELECT DISTINCT ON (feature) feature, ts, value, aux_values
    FROM core.macro_ticks
    WHERE feature = ANY(%s)
    ORDER BY feature, ts DESC;
    """
    with psycopg2.connect(**pg_creds()) as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql, (features,))
        rows = cur.fetchall()
    out = {}
    for r in rows:
        out[r["feature"]] = {
            "ts": r["ts"],
            "value": float(r["value"]) if r["value"] is not None else None,
            "meta": r["aux_values"] or {},
        }
    return out

def fundamentals_snapshot() -> Dict[str, Any]:
    data = read_latest(NEEDED)
    # placeholder para news_block: lo conectamos luego
    data["_news"] = {"block": False, "next": None}
    return data
