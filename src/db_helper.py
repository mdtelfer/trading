# src/db_helpers.py
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from psycopg2.extras import Json


def upsert_macro_tick(
    cur,
    ts,
    feature: str,
    value,
    aux: Mapping[str, Any] | None,
    source_id: str,
    method: str,
    status: str = "healthy",
    valid_until=None,
    lat_ms: int = 0,
):
    """
    Upsert único y consistente con la firma SQL:
    (p_ts, p_feature, p_value, p_aux_values, p_source_id, p_method, p_status, p_valid_until_ts, p_ingest_latency_ms)
    """
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
            value=value,
            aux=Json(aux or {}),
            source_id=source_id,
            method=method,
            status=status,
            valid_until=valid_until,
            lat_ms=lat_ms,
        ),
    )
