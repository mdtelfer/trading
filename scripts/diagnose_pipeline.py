# scripts/diagnose_pipeline.py
from __future__ import annotations
import sys
from datetime import datetime
import psycopg2
from psycopg2.extras import Json
from src.log import get_logger
from src.config import load_yaml, env
from src.router import mt5_features_from_yaml
from src.sources.mt5_source import get_tick

log = get_logger("diagnose")

def pg_creds() -> dict:
    return dict(
        host=env("DB_HOST", "localhost"),
        port=int(env("DB_PORT", "5432")),
        dbname=env("DB_NAME", "trading"),
        user=env("DB_USER", "postgres"),
        password=env("DB_PASSWORD", "postgres"),
    )

def check_db(conn):
    with conn.cursor() as cur:
        # tabla
        cur.execute("""
            SELECT to_regclass('core.macro_ticks') IS NOT NULL;
        """)
        has_table = cur.fetchone()[0]
        log.info(f"core.macro_ticks existe: {has_table}")

        # función
        cur.execute("""
            SELECT EXISTS (
              SELECT 1
              FROM pg_proc p
              JOIN pg_namespace n ON n.oid = p.pronamespace
              WHERE p.proname = 'upsert_macro_tick'
                AND n.nspname = 'core'
            );
        """)
        has_fn = cur.fetchone()[0]
        log.info(f"core.upsert_macro_tick existe: {has_fn}")

        # conteo
        cur.execute("SELECT count(*) FROM core.macro_ticks;")
        count = cur.fetchone()[0]
        log.info(f"filas actuales en core.macro_ticks: {count}")

        return has_table, has_fn

def do_upsert(conn, feature: str, tick: dict):
    from datetime import datetime
    ts = datetime.fromisoformat(tick["time"])
    bid = float(tick["bid"]); ask = float(tick["ask"])
    mid = (bid + ask) / 2.0
    aux = {"symbol": tick["symbol"], "bid": bid, "ask": ask, "spread": max(0.0, ask-bid)}
    with conn.cursor() as cur:
        cur.execute("""
            SELECT core.upsert_macro_tick(
                %(ts)s, %(feature)s, %(value)s, %(aux)s,
                %(source_id)s, %(method)s, %(status)s,
                %(valid_until)s, %(lat_ms)s
            );
        """, dict(
            ts=ts, feature=feature, value=mid, aux=Json(aux),
            source_id="mt5", method="unofficial_api", status="healthy",
            valid_until=ts, lat_ms=0
        ))
    conn.commit()

def main():
    # DB
    creds = pg_creds()
    conn = psycopg2.connect(**creds)
    log.info(f"Conectado a DB {creds['host']}:{creds['port']}/{creds['dbname']}")
    has_table, has_fn = check_db(conn)
    if not has_table or not has_fn:
        log.error("Falta tabla o función. Aplica el patch SQL y reintenta.")
        sys.exit(1)

    # YAML
    cfg = load_yaml()
    f2s = mt5_features_from_yaml(cfg)
    log.info(f"Features MT5 en YAML: {f2s}")
    if not f2s:
        log.error("No hay features MT5 en el YAML (primary.type: mt5). Revisa configs/fundamental_sources.yaml")
        sys.exit(1)

    # MT5 → un tick del primer símbolo
    first_feat, first_sym = next(iter(f2s.items()))
    log.info(f"Probando MT5 tick para {first_feat} -> {first_sym}")
    t = get_tick(first_sym)
    log.info(f"Tick obtenido: {t}")
    if not t:
        log.error("No se pudo obtener tick desde MT5 (revisa conexión, símbolo o mapping).")
        sys.exit(1)

    # UPSERT
    do_upsert(conn, first_feat, t)
    log.info("UPSERT OK. Verificando lectura…")
    with conn.cursor() as cur:
        cur.execute("""
            SELECT ts, feature, value, aux_values->>'symbol' AS sym
            FROM core.macro_ticks
            WHERE feature = %s
            ORDER BY ts DESC
            LIMIT 5;
        """, (first_feat,))
        rows = cur.fetchall()
        for r in rows:
            log.info(f"row: {r}")

    conn.close()
    log.info("Diagnóstico completado sin errores ✔")

if __name__ == "__main__":
    main()
