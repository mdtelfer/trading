# scripts/run_mt5_to_macro.py
from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from datetime import datetime
import time
from typing import Any, TypedDict

import MetaTrader5 as mt5
import psycopg2
from psycopg2.extensions import connection as PGConnection, cursor as PGCursor
from psycopg2.extras import Json
from src.config import env, load_yaml
from src.log import get_logger, init_logger
from src.router import get_all_mt5_ticks
from src.sources.mt5_source import MT5  # para usar connect/attach

# ------------------------------- Tipos ---------------------------------------


class MT5Tick(TypedDict):
    symbol: str
    bid: float
    ask: float
    time: str  # ISO-8601 con TZ


# ------------------------------- Utilidades ----------------------------------
init_logger()
log = get_logger("run_mt5_to_macro")


def _env_str(name: str, default: str) -> str:
    """Lee un str del entorno con default seguro."""
    v: str | None = env(name, default)
    return v if v is not None else default


def _env_int(name: str, default: int) -> int:
    """Lee un int del entorno con parsing y default seguro."""
    v: str | None = env(name, None)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def wait_for_mt5_ready(timeout_sec: int = 60) -> bool:
    """
    Espera a que la GUI de MT5 esté abierta y logueada (attach-only).
    Devuelve True si se pudo conectar a una sesión activa.
    """
    start = time.time()
    while time.time() - start < float(timeout_sec):
        try:
            # intenta engancharse sin abrir otra instancia
            MT5.connect(retries=1, wait_sec=0.5)
            acct = (
                mt5.account_info()
            )  # pyright: ignore[reportUnknownMemberType, reportAttributeAccessIssue]
            if acct and acct.login:  # type: ignore
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def pg_creds() -> dict[str, Any]:
    """Credenciales PG desde .env (con defaults)."""
    return dict(
        host=_env_str("DB_HOST", "localhost"),
        port=_env_int("DB_PORT", 5432),
        dbname=_env_str("DB_NAME", "trading"),
        user=_env_str("DB_USER", "postgres"),
        password=_env_str("DB_PASSWORD", "postgres"),
    )


def upsert_tick(conn: PGConnection, feature: str, tick: Mapping[str, Any]) -> None:
    """
    Inserta/actualiza un tick MT5 en core.macro_ticks vía core.upsert_macro_tick().
    tick: {'symbol','bid','ask','time'(ISO-8601 con TZ)}
    """
    # parsea el timestamp ISO (viene en UTC desde mt5_source)
    ts = datetime.fromisoformat(str(tick["time"]))
    bid = float(tick["bid"])
    ask = float(tick["ask"])
    mid = (bid + ask) / 2.0

    aux = {
        "symbol": str(tick["symbol"]),
        "bid": bid,
        "ask": ask,
        "spread": max(0.0, ask - bid),
    }

    cur: PGCursor
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT core.upsert_macro_tick(
                %(ts)s, %(feature)s, %(value)s, %(aux)s,
                %(source_id)s, %(method)s, %(status)s,
                %(valid_until)s, %(lat_ms)s
            );
            """,
            dict(
                ts=ts,
                feature=feature,
                value=mid,
                aux=Json(aux),
                source_id="mt5",
                method="unofficial_api",
                status="healthy",
                valid_until=ts,  # para ticks en tiempo real, usamos el mismo ts
                lat_ms=0,
            ),
        )
    conn.commit()


def job(conn: PGConnection) -> None:
    """
    Lee el YAML, obtiene los ticks MT5 mapeados a features y hace upsert en la base.
    """
    cfg = load_yaml()
    ticks_by_feature: dict[str, MT5Tick | None] = get_all_mt5_ticks(cfg)

    if not ticks_by_feature:
        log.warning("No MT5 ticks fetched (¿sin features MT5 en YAML?).")
        return

    total = len(ticks_by_feature)
    inserted = 0
    for feature, tick in ticks_by_feature.items():
        if not tick:
            log.debug(f"[skip] {feature}: tick=None")
            continue
        try:
            upsert_tick(conn, feature, tick)
            inserted += 1
        except Exception as e:
            log.exception(f"Error upserting {feature}: {e}")

    log.info(f"MT5 cycle: features={total} inserted={inserted} skipped={total - inserted}")


def main() -> None:
    if not wait_for_mt5_ready(timeout_sec=90):
        log.error("MT5 no está listo (GUI no abierta o no logueada). Abre MT5 GUI y reintenta.")
        return

    interval: int = _env_int("MT5_POLL_INTERVAL_SEC", 5)
    creds: dict[str, Any] = pg_creds()

    # Conexión persistente a la DB
    conn: PGConnection = psycopg2.connect(**creds)
    log.info(
        f"MT5 → Timescale poller iniciado. Intervalo={interval}s | "
        f"DB={creds['host']}:{creds['port']}/{creds['dbname']}"
    )

    try:
        while True:
            try:
                job(conn)
            except Exception as e:
                log.exception(f"Fallo en ciclo de polling: {e}")
                with suppress(Exception):
                    conn.close()
                time.sleep(2)
                conn = psycopg2.connect(**creds)

            time.sleep(float(interval))

    finally:
        with suppress(Exception):
            conn.close()


if __name__ == "__main__":
    main()
