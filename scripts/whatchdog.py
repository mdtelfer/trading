from __future__ import annotations

from contextlib import closing
from datetime import UTC, datetime, timedelta
import os
import time
from typing import Any

import psycopg2
from psycopg2.extras import Json

from src.log import get_logger, init_logger

# ---------------- logging ----------------
init_logger()
log = get_logger("watchdog")

# ---------------- DB env -----------------
DB = dict(
    host=os.getenv("DB_HOST", "localhost"),
    port=int(os.getenv("DB_PORT", "5432")),
    dbname=os.getenv("DB_NAME", "trading"),
    user=os.getenv("DB_USER", "postgres"),
    password=os.getenv("DB_PASSWORD", "postgres"),
)

EVERY_SEC = int(os.getenv("WATCHDOG_EVERY_SEC", "60"))

# Thresholds por defecto (puedes sobreescribir por env)
FEATURE_THR_SEC = int(os.getenv("FEATURE_THRESHOLD_SEC", "900"))  # 15 min
FAST_EVERY = int(os.getenv("FAST_EVERY_SEC", "300"))  # 5 min
SLOW_EVERY = int(os.getenv("SLOW_EVERY_SEC", "3600"))  # 1 h
FAST_THR = int(os.getenv("FAST_TIER_THRESHOLD_SEC", str(int(2.5 * FAST_EVERY))))
SLOW_THR = int(os.getenv("SLOW_TIER_THRESHOLD_SEC", str(int(2.5 * SLOW_EVERY))))
FUSED_THR = int(os.getenv("FUSED_TIER_THRESHOLD_SEC", str(SLOW_THR)))

# Supresión local (evita spam)
SUPPRESS_MIN = int(os.getenv("SUPPRESS_MINUTES", "30"))
_last_alert: dict[str, datetime] = {}


def now_utc() -> datetime:
    return datetime.now(UTC)


def should_suppress(key: str) -> bool:
    last = _last_alert.get(key)
    return bool(last and (now_utc() - last) < timedelta(minutes=SUPPRESS_MIN))


def mark_alert(key: str):
    _last_alert[key] = now_utc()


def insert_alert(conn, kind: str, key: str, age_sec: int, thr_sec: int, payload: dict[str, Any]):
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.alerts (kind, key, age_sec, threshold_sec, payload)
            VALUES (%(k)s, %(key)s, %(a)s, %(t)s, %(p)s)
            """,
            dict(k=kind, key=key, a=age_sec, t=thr_sec, p=Json(payload)),
        )
    conn.commit()


def check_features(conn) -> int:
    """
    Calcula age_sec por feature desde v_macro_latest y alerta si supera FEATURE_THR_SEC.
    No require v_feature_freshness; lo derivamos on the fly.
    """
    # Nota: usamos age desde v_macro_latest para minimizar dependencias
    sql = """
      SELECT l.feature,
             l.ts,
             EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'UTC' - l.ts))::bigint AS age_sec,
             l.status,
             COALESCE(l.aux_values->>'expected_source', l.aux_values->>'source', '') AS source_hint
      FROM core.v_macro_latest l
    """
    with conn.cursor() as cur:
        cur.execute(sql)
        rows = cur.fetchall()
    alerts = 0
    for feature, ts, age_sec, status, source in rows:
        age = int(age_sec or 0)
        if age > FEATURE_THR_SEC:
            key = f"feature:{feature}"
            if should_suppress(key):
                continue
            payload = {"ts": str(ts), "status": status, "source": source}
            log.warning(
                "⚠️ feature_lag | %s age=%ss > thr=%ss | %s", feature, age, FEATURE_THR_SEC, payload
            )
            insert_alert(conn, "feature_lag", feature, age, FEATURE_THR_SEC, payload)
            mark_alert(key)
            alerts += 1
    return alerts


def check_evaluator(conn) -> int:
    """
    Lee v_evaluator_freshness (tier, ts, age_sec) y compara con thresholds.
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT tier, ts, EXTRACT(EPOCH FROM (NOW() AT TIME ZONE 'UTC' - ts))::bigint AS age_sec FROM core.v_evaluator_freshness"
        )
        rows = cur.fetchall()
    thr = {"fast": FAST_THR, "slow": SLOW_THR, "fused": FUSED_THR}
    seen = set()
    alerts = 0
    for tier, ts, age_sec in rows:
        seen.add(tier)
        age = int(age_sec or 0)
        t = int(thr.get(tier, SLOW_THR))
        if age > t:
            key = f"evaluator:{tier}"
            if should_suppress(key):
                continue
            payload = {"ts": str(ts)}
            log.warning("⚠️ evaluator_idle | %s age=%ss > thr=%ss | %s", tier, age, t, payload)
            insert_alert(conn, "evaluator_idle", tier, age, t, payload)
            mark_alert(key)
            alerts += 1
    # si falta algún tier por completo
    for missing in {"fast", "slow", "fused"} - seen:
        key = f"evaluator:{missing}:missing"
        if should_suppress(key):
            continue
        log.warning("⚠️ evaluator_missing | %s no tiene registros en core.macro_state", missing)
        insert_alert(conn, "evaluator_missing", missing, 0, 0, {"note": "no rows"})
        mark_alert(key)
        alerts += 1
    return alerts


def main():
    log.info(
        "Watchdog iniciado | every=%ss | FEATURE_THR=%ss | fast/slow/fused=%s/%s/%s",
        EVERY_SEC,
        FEATURE_THR_SEC,
        FAST_THR,
        SLOW_THR,
        FUSED_THR,
    )
    while True:
        try:
            with closing(psycopg2.connect(**DB)) as conn:
                n = 0
                n += check_features(conn)
                n += check_evaluator(conn)
                if n == 0:
                    log.debug("OK (sin alertas).")
        except Exception as e:
            log.exception("Error en watchdog: %s", e)
        time.sleep(EVERY_SEC)


if __name__ == "__main__":
    main()
