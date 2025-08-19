from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import psycopg2
import yaml

from src.config import env
from src.log import get_logger
from src.pipeline.snapshot import fundamentals_snapshot  # usa core.macro_ticks

log = get_logger("fund_gate")


def pg_creds():
    return dict(
        host=env("DB_HOST", "localhost"),
        port=int(env("DB_PORT", "5432")),
        dbname=env("DB_NAME", "trading"),
        user=env("DB_USER", "postgres"),
        password=env("DB_PASSWORD", "postgres"),
    )


def load_rules() -> dict[str, Any]:
    path = Path("configs/fundamental_rules.yaml")
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def evaluate_rules(snap: dict[str, Any], rules: dict[str, Any]) -> tuple[bool, str, list[str]]:
    missing: list[str] = []
    req = rules.get("required_features", [])
    for f in req:
        if f not in snap or snap[f].get("value") is None:
            missing.append(f)

    if missing:
        return False, f"Missing features: {','.join(missing)}", missing

    # macro
    macro = rules.get("macro", {})
    vix_max = (macro.get("VIX") or {}).get("max")
    ust_max = (macro.get("UST10Y") or {}).get("max")
    dxy_min = (macro.get("DXY") or {}).get("min")
    dxy_max = (macro.get("DXY") or {}).get("max")

    vix = snap["VIX"]["value"]
    ust = snap["UST10Y"]["value"]  # decimal
    dxy = snap["DXY"]["value"]

    if vix_max is not None and vix > vix_max:
        return False, f"VIX>{vix_max}", []
    if ust_max is not None and ust > ust_max:
        return False, f"UST10Y>{ust_max}", []
    if dxy_min is not None and dxy < dxy_min:
        return False, f"DXY<{dxy_min}", []
    if dxy_max is not None and dxy > dxy_max:
        return False, f"DXY>{dxy_max}", []

    # micro / news
    news_cfg = (rules.get("micro") or {}).get("news_block", {})
    if news_cfg.get("enabled", True) and snap.get("_news", {}).get("block", False):
        return False, "NewsBlock", []

    return True, "OK", []


def _sanitize(obj):
    # Convierte datetimes a ISO-8601 recursivamente
    if isinstance(obj, dict):
        return {k: _sanitize(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_sanitize(x) for x in obj]
    # Datetime/Date → isoformat
    try:
        import datetime as _dt

        if isinstance(obj, (_dt.datetime, _dt.date)):
            return obj.isoformat()
    except Exception:
        pass
    return obj


def audit_decision(
    signal: dict[str, Any], snap: dict[str, Any], allowed: bool, reason: str, missing: list[str]
) -> None:
    from psycopg2.extras import Json

    snap_clean = _sanitize(snap)
    signal_clean = _sanitize(signal)

    # dumps custom para garantizar serialización (por si queda algo raro)
    def dumps(o):
        return json.dumps(o, ensure_ascii=False, default=str)

    with psycopg2.connect(**pg_creds()) as conn, conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO core.fund_gate_audit (allowed, reason, feature_missing, snapshot, signal)
            VALUES (%s,%s,%s,%s,%s)
        """,
            (
                allowed,
                reason,
                missing,
                Json(snap_clean, dumps=dumps),
                Json(signal_clean, dumps=dumps),
            ),
        )
        conn.commit()


def allow_trade(signal: dict[str, Any]) -> tuple[bool, str]:
    """
    signal (ejemplo): {
      "symbol":"XAUUSD","side":"buy","entry":..., "sl":..., "tp":..., "source":"tradingview", ...
    }
    """
    rules = load_rules()
    snap = fundamentals_snapshot()  # ya está leyendo macro_ticks
    ok, reason, missing = evaluate_rules(snap, rules)
    audit_decision(signal, snap, ok, reason, missing)
    log.info(f"[FUND-GATE] allowed={ok} reason={reason} missing={missing}")
    return ok, reason
