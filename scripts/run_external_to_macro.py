# scripts/run_external_to_macro.py
from __future__ import annotations

from collections.abc import Mapping
from contextlib import suppress
from datetime import UTC, datetime, timedelta
import time
from typing import Any, Final, TypedDict, cast

import psycopg2
from psycopg2.extensions import connection as PGConnection
from psycopg2.extras import Json

from src.config import env, load_yaml
from src.log import get_logger, init_logger

# Fuentes externas / helpers ya existentes en tu proyecto
from src.sources.altme_source import get_crypto_fng
from src.sources.cboe_source import get_total_pcr
from src.sources.cnn_fng_source import fetch_fng
from src.sources.ff_calendar import compute_news_block
from src.sources.fred_source import get_oas_from_fred, get_series_latest, get_ust10y_fred
from src.sources.gpr_source import get_gpr_global
from src.sources.yahoo_source import (
    get_breadth_spx_adv_dec,
    get_breadth_spx_pct_above,
    get_copper_quote,
    get_dxy_quote,
    get_gold_fut_quote,
    get_gvz_quote,
    get_hyg_quote,
    get_lqd_quote,
    get_move_quote,
    get_ovx_quote,
    get_rel_volume_20d,
    get_skew_quote,
    get_ust10y_from_yahoo,
    get_vix_quote,
    get_vxn_quote,
)

init_logger()
log = get_logger("run_external_to_macro")

log.info("External poller iniciado.")
# ------------------------------- Tipos auxiliares ----------------------------

MacroCfg = Mapping[str, Any]
YamlCfg = Mapping[str, Any]


# ---- Tipos de respuesta mínimos para silenciar Pylance ----
class QuoteBasic(TypedDict):
    symbol: str
    value: float
    time: str
    date: str


class QuoteWithPct(QuoteBasic, total=False):
    value_dec: float
    value_pct: float
    source: str
    label: str
    period: str
    row: dict[str, Any]
    next_event: dict[str, Any]


class FredObs(TypedDict):
    symbol: str
    value: float
    time: str
    date: str


class YahooTNXObs(TypedDict):
    symbol: str
    time: str
    value_dec: float  # requerido
    value_pct: float


# ------------------------------- Utilidades ----------------------------------


def _env_str(name: str, default: str) -> str:
    v: str | None = env(name, default)
    return v if v is not None else default


def _env_int(name: str, default: int) -> int:
    v: str | None = env(name, None)
    try:
        return int(v) if v is not None else default
    except (TypeError, ValueError):
        return default


def _now_utc() -> datetime:
    """Ahora en UTC, tz-aware."""
    return datetime.now(UTC)


def _iso_to_dt(ts_iso: str) -> datetime:
    """Convierte ISO8601 a datetime tz-aware (preservando tz si viene)."""
    dt = datetime.fromisoformat(ts_iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def _pg_creds() -> dict[str, Any]:
    """Credenciales PG desde .env (con defaults)."""
    return dict(
        host=_env_str("DB_HOST", "localhost"),
        port=_env_int("DB_PORT", 5432),
        dbname=_env_str("DB_NAME", "trading"),
        user=_env_str("DB_USER", "postgres"),
        password=_env_str("DB_PASSWORD", "postgres"),
    )


def _yaml_source_is(macro: MacroCfg, feature: str, source: str) -> bool:
    """Devuelve True si en YAML: layers.macro.<feature>.primary.source == source."""
    try:
        s = macro.get(feature, {}).get("primary", {}).get("source")
        return bool(s == source)
    except Exception:
        return False


def _yaml_symbol(macro: MacroCfg, feature: str, default: str | None = None) -> str | None:
    """Obtiene el símbolo de YAML: layers.macro.<feature>.primary.symbol."""
    try:
        sym = macro.get(feature, {}).get("primary", {}).get("symbol")
        return sym if sym else default
    except Exception:
        return default


def _cfg_dict(m: Mapping[str, Any], key: str) -> dict[str, Any]:
    v = m.get(key, {})
    return cast(dict[str, Any], v or {})


# Cadencias en segundos (con tipos para mypy)
CADENCES: Final[dict[str, int]] = {
    "VIX": 60,
    "DXY": 60,
    "UST10Y": 300,  # FRED (diario) / Yahoo ^TNX intradía (fallback)
    "FEAR_GREED_CRYPTO": 900,  # 15 min
    "PCR": 3600,  # 1h (EOD/near-EOD)
    "GPR": 43200,  # 12h (mensual)
    "CREDIT_SPREAD_HY": 3600,
    "CREDIT_SPREAD_IG": 3600,
    "CALENDAR_BLOCK": 300,
    "FNG_STOCKS": 900,
    "MOVE": 300,
    "GVZ": 300,
    "OVX": 300,
    "VXN": 300,
    "SKEW": 900,
    "UST2Y": 300,
    "UST30Y": 300,
    "T10YIE": 3600,
    "REAL10Y": 3600,
    # Liquidez
    "NFCI": 43200,  # semanal: check 2 veces al día
    "SOFR": 43200,  # diario
    "ON_RRP": 43200,  # diario
    # Crédito (niveles)
    "DBAA": 3600,
    "DAAA": 3600,
    # Commodities / ETFs
    "COPPER": 900,
    "GOLD_FUT": 900,
    "HYG": 900,
    "LQD": 900,
    # Derivados (calculados)
    "SPREAD_2S10S": 300,
    "HYG_LQD_RATIO": 900,
    "COPPER_GOLD_RATIO": 900,
    "BREADTH_SPX_PCT_ABOVE_200D": 1800,  # 30 min
    "BREADTH_SPX_PCT_ABOVE_50D": 1800,  # 30 min
    "BREADTH_SPX_ADV_DEC": 900,  # 15 min
    "RELVOL_SPY_20D": 300,  # 5 min
    "RELVOL_QQQ_20D": 300,
    "RELVOL_HYG_20D": 300,
    "RELVOL_LQD_20D": 300,
}


def _due(last_run: dict[str, datetime], name: str, now: datetime) -> bool:
    """¿Toca ejecutar name? (según CADENCES y last_run)."""
    cad = CADENCES.get(name)
    if cad is None:
        return False
    prev = last_run.get(name)
    return (prev is None) or ((now - prev) >= timedelta(seconds=cad))


def _mark(last_run: dict[str, datetime], name: str, now: datetime) -> None:
    last_run[name] = now


def _fred_latest(series_id: str) -> tuple[float | None, str]:
    """Devuelve (valor, ts_iso) para una serie FRED (usa now si no viene ts)."""
    v = get_series_latest(series_id)
    if v is None:
        return None, _now_utc().isoformat()
    return float(v), _now_utc().isoformat()


def _upsert_simple_fred(conn: PGConnection, feature: str, series_id: str) -> bool:
    val, ts = _fred_latest(series_id)
    if val is None:
        return False
    upsert_value(
        conn,
        feature=feature,
        symbol=series_id,
        value=float(val),
        ts_iso=ts,
        extra={"unit": "level"},
        source_id="fred",
        method="official_api",
    )
    log.info(f"{feature} {val} (FRED:{series_id})")
    return True


def _upsert_yahoo_price(conn: PGConnection, feature: str, getter) -> bool:
    q = getter()
    if not q:
        return False
    upsert_value(
        conn,
        feature=feature,
        symbol=str(q["symbol"]),
        value=float(q["value"]),
        ts_iso=str(q["time"]),
        source_id="yfinance",
        method="unofficial_api",
    )
    log.info(f"{feature} {q['value']}")
    return True


# ------------------------------- Upsert --------------------------------------


def upsert_value(
    conn: PGConnection,
    feature: str,
    symbol: str,
    value: float,
    ts_iso: str,
    extra: dict[str, Any] | None = None,
    source_id: str = "external",
    method: str = "unofficial_api",
) -> None:
    """
    Inserta/actualiza en core.macro_ticks usando core.upsert_macro_tick.
    - feature: nombre del feature (ej. 'VIX')
    - symbol: símbolo/serie (ej. '^VIX', 'DGS10', etc.)
    - value: valor numérico
    - ts_iso: timestamp ISO (tz-aware recomendado)
    - extra: dict con metadatos (unit, notes, etc.)
    Validez: valid_until = ts + (cadencia_del_feature * 2) para evitar 'expired' inmediato.
    """
    ts = _iso_to_dt(ts_iso)
    # TTL: 2x la cadencia configurada (fallback 5 min)
    ttl_sec = CADENCES.get(feature, 300)
    valid_until_dt = ts + timedelta(seconds=ttl_sec * 2)

    aux: dict[str, Any] = {"symbol": symbol, "source": source_id}
    if extra:
        aux.update(extra)

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
                value=float(value),
                aux=Json(aux),
                source_id=source_id,
                method=method,
                status="healthy",
                valid_until=valid_until_dt,
                lat_ms=0,
            ),
        )
    conn.commit()


def main() -> None:  # noqa: C901
    # ---------- 👇 INICIALIZAR LOGGER ANTES DE CUALQUIER log.info ----------

    cfg: YamlCfg = load_yaml()
    creds: dict[str, Any] = _pg_creds()
    conn: PGConnection = psycopg2.connect(**creds)
    log.info("External poller iniciado.")

    last_run: dict[str, datetime] = {}

    try:
        while True:
            now = _now_utc()

            # ---- obtener layers y macro con casts intermedios ----
            layers: dict[str, Any] = cast(
                dict[str, Any],
                cfg.get("layers", {}) or {},  # pyright: ignore[reportUnknownMemberType]
            )
            macro: MacroCfg = cast(MacroCfg, layers.get("macro", {}) or {})

            try:
                # --- MOVE ---
                if _due(last_run, "MOVE", now) and _yaml_source_is(macro, "MOVE", "YahooFinance"):
                    q = get_move_quote()
                    if q:
                        upsert_value(
                            conn,
                            "MOVE",
                            str(q["symbol"]),
                            float(q["value"]),
                            str(q["time"]),
                            source_id="yfinance",
                        )
                        log.info(f"MOVE {q['value']}")
                    _mark(last_run, "MOVE", now)

                # --- GVZ ---
                if _due(last_run, "GVZ", now) and _yaml_source_is(macro, "GVZ", "YahooFinance"):
                    q = get_gvz_quote()
                    if q:
                        upsert_value(
                            conn,
                            "GVZ",
                            str(q["symbol"]),
                            float(q["value"]),
                            str(q["time"]),
                            source_id="yfinance",
                        )
                        log.info(f"GVZ {q['value']}")
                    _mark(last_run, "GVZ", now)

                # --- OVX ---
                if _due(last_run, "OVX", now) and _yaml_source_is(macro, "OVX", "YahooFinance"):
                    q = get_ovx_quote()
                    if q:
                        upsert_value(
                            conn,
                            "OVX",
                            str(q["symbol"]),
                            float(q["value"]),
                            str(q["time"]),
                            source_id="yfinance",
                        )
                        log.info(f"OVX {q['value']}")
                    _mark(last_run, "OVX", now)

                # --- VXN ---
                if _due(last_run, "VXN", now) and _yaml_source_is(macro, "VXN", "YahooFinance"):
                    q = get_vxn_quote()
                    if q:
                        upsert_value(
                            conn,
                            "VXN",
                            str(q["symbol"]),
                            float(q["value"]),
                            str(q["time"]),
                            source_id="yfinance",
                        )
                        log.info(f"VXN {q['value']}")
                    _mark(last_run, "VXN", now)

                # --- SKEW ---
                if _due(last_run, "SKEW", now) and _yaml_source_is(macro, "SKEW", "YahooFinance"):
                    q = get_skew_quote()
                    if q:
                        upsert_value(
                            conn,
                            "SKEW",
                            str(q["symbol"]),
                            float(q["value"]),
                            str(q["time"]),
                            source_id="yfinance",
                        )
                        log.info(f"SKEW {q['value']}")
                    _mark(last_run, "SKEW", now)

                # --- UST2Y ---
                if _due(last_run, "UST2Y", now) and _yaml_source_is(macro, "UST2Y", "FRED"):
                    _upsert_simple_fred(conn, "UST2Y", macro["UST2Y"]["primary"]["series"])
                    _mark(last_run, "UST2Y", now)

                # --- UST30Y ---
                if _due(last_run, "UST30Y", now) and _yaml_source_is(macro, "UST30Y", "FRED"):
                    _upsert_simple_fred(conn, "UST30Y", macro["UST30Y"]["primary"]["series"])
                    _mark(last_run, "UST30Y", now)

                # --- 10y breakeven (T10YIE) ---
                if _due(last_run, "T10YIE", now) and _yaml_source_is(macro, "T10YIE", "FRED"):
                    _upsert_simple_fred(conn, "T10YIE", macro["T10YIE"]["primary"]["series"])
                    _mark(last_run, "T10YIE", now)

                # --- 10y real (DFII10) ---
                if _due(last_run, "REAL10Y", now) and _yaml_source_is(macro, "REAL10Y", "FRED"):
                    _upsert_simple_fred(conn, "REAL10Y", macro["REAL10Y"]["primary"]["series"])
                    _mark(last_run, "REAL10Y", now)

                # --- VIX (Yahoo) ---
                if _due(last_run, "VIX", now) and _yaml_source_is(macro, "VIX", "YahooFinance"):
                    q_vix_raw = get_vix_quote()
                    if q_vix_raw:
                        q_vix: QuoteBasic = cast(QuoteBasic, q_vix_raw)
                        upsert_value(
                            conn,
                            "VIX",
                            str(q_vix["symbol"]),
                            float(q_vix["value"]),
                            str(q_vix["time"]),
                            source_id="yfinance",
                        )
                        log.info(f"VIX {q_vix['value']}")
                    _mark(last_run, "VIX", now)

                # --- DXY (Yahoo) ---
                if _due(last_run, "DXY", now) and _yaml_source_is(macro, "DXY", "YahooFinance"):
                    yaml_sym = _yaml_symbol(macro, "DXY")
                    symbols = [yaml_sym] if yaml_sym else None
                    q_dxy_raw = get_dxy_quote(symbols)
                    if q_dxy_raw:
                        q_dxy: QuoteBasic = cast(QuoteBasic, q_dxy_raw)
                        upsert_value(
                            conn,
                            "DXY",
                            str(q_dxy["symbol"]),
                            float(q_dxy["value"]),
                            str(q_dxy["time"]),
                            source_id="yfinance",
                        )
                        log.info(f"DXY {q_dxy['value']} ({q_dxy['symbol']})")
                    _mark(last_run, "DXY", now)

                # --- COPPER (HG=F) ---
                if _due(last_run, "COPPER", now) and _yaml_source_is(
                    macro, "COPPER", "YahooFinance"
                ):
                    _upsert_yahoo_price(conn, "COPPER", get_copper_quote)
                    _mark(last_run, "COPPER", now)

                # --- GOLD_FUT (GC=F) ---
                if _due(last_run, "GOLD_FUT", now) and _yaml_source_is(
                    macro, "GOLD_FUT", "YahooFinance"
                ):
                    _upsert_yahoo_price(conn, "GOLD_FUT", get_gold_fut_quote)
                    _mark(last_run, "GOLD_FUT", now)

                # --- HYG ---
                if _due(last_run, "HYG", now) and _yaml_source_is(macro, "HYG", "YahooFinance"):
                    _upsert_yahoo_price(conn, "HYG", get_hyg_quote)
                    _mark(last_run, "HYG", now)

                # --- LQD ---
                if _due(last_run, "LQD", now) and _yaml_source_is(macro, "LQD", "YahooFinance"):
                    _upsert_yahoo_price(conn, "LQD", get_lqd_quote)
                    _mark(last_run, "LQD", now)

                # --- UST10Y (FRED -> decimal; fallback Yahoo ^TNX -> decimal) ---
                if _due(last_run, "UST10Y", now) and _yaml_source_is(macro, "UST10Y", "FRED"):
                    ust10y_cfg: dict[str, Any] = cast(dict[str, Any], macro.get("UST10Y", {}) or {})
                    primary_cfg: dict[str, Any] = cast(
                        dict[str, Any], ust10y_cfg.get("primary", {}) or {}
                    )
                    series: str = str(primary_cfg.get("series", "DGS10"))

                    q_fred_raw = get_ust10y_fred(series)
                    if q_fred_raw:
                        q_fred: FredObs = cast(FredObs, q_fred_raw)
                        pct = float(q_fred["value"])
                        dec = round(pct / 100.0, 5)
                        upsert_value(
                            conn,
                            "UST10Y",
                            str(q_fred["symbol"]),
                            dec,
                            str(q_fred["time"]),
                            extra={
                                "fred_date": q_fred["date"],
                                "value_pct": pct,
                                "unit": {"stored": "decimal", "original": "percent"},
                            },
                            source_id="fred",
                            method="official_api",
                        )
                        log.info(f"UST10Y {pct}% (stored={dec}) FRED")
                    else:
                        # Fallback: Yahoo ^TNX
                        q_tnx_raw = get_ust10y_from_yahoo("^TNX")
                        if q_tnx_raw:
                            q_tnx: YahooTNXObs = cast(YahooTNXObs, q_tnx_raw)
                            dec2 = round(float(q_tnx["value_dec"]), 5)
                            upsert_value(
                                conn,
                                "UST10Y",
                                str(q_tnx["symbol"]),
                                dec2,
                                str(q_tnx["time"]),
                                extra={
                                    "value_pct": float(q_tnx["value_pct"]),
                                    "note": "from ^TNX (Yahoo)",
                                    "unit": {"stored": "decimal", "original": "percent"},
                                },
                                source_id="yfinance",
                                method="unofficial_api",
                            )
                            log.info(f"UST10Y {q_tnx['value_pct']}% (stored={dec2}) Yahoo")
                    _mark(last_run, "UST10Y", now)

                # --- NFCI ---
                if _due(last_run, "NFCI", now) and _yaml_source_is(macro, "NFCI", "FRED"):
                    _upsert_simple_fred(conn, "NFCI", macro["NFCI"]["primary"]["series"])
                    _mark(last_run, "NFCI", now)

                # --- SOFR ---
                if _due(last_run, "SOFR", now) and _yaml_source_is(macro, "SOFR", "FRED"):
                    _upsert_simple_fred(conn, "SOFR", macro["SOFR"]["primary"]["series"])
                    _mark(last_run, "SOFR", now)

                # --- ON RRP ---
                if _due(last_run, "ON_RRP", now) and _yaml_source_is(macro, "ON_RRP", "FRED"):
                    _upsert_simple_fred(conn, "ON_RRP", macro["ON_RRP"]["primary"]["series"])
                    _mark(last_run, "ON_RRP", now)

                # --- DBAA ---
                if _due(last_run, "DBAA", now) and _yaml_source_is(macro, "DBAA", "FRED"):
                    _upsert_simple_fred(conn, "DBAA", macro["DBAA"]["primary"]["series"])
                    _mark(last_run, "DBAA", now)

                # --- DAAA ---
                if _due(last_run, "DAAA", now) and _yaml_source_is(macro, "DAAA", "FRED"):
                    _upsert_simple_fred(conn, "DAAA", macro["DAAA"]["primary"]["series"])
                    _mark(last_run, "DAAA", now)

                # --- FEAR_GREED_CRYPTO (Alternative.me) ---
                if _due(last_run, "FEAR_GREED_CRYPTO", now):
                    q_crypto_raw = get_crypto_fng()
                    if q_crypto_raw:
                        q_crypto: QuoteWithPct = cast(QuoteWithPct, q_crypto_raw)
                        upsert_value(
                            conn,
                            "FEAR_GREED_CRYPTO",
                            str(q_crypto["symbol"]),
                            float(q_crypto["value"]),
                            str(q_crypto["time"]),
                            extra={"label": q_crypto.get("label")},
                            source_id="alternative_me",
                            method=(
                                "official_api"
                                if q_crypto.get("source") == "official"
                                else "unofficial_api"
                            ),
                        )
                        log.info(f"CRYPTO F&G {q_crypto['value']} ({q_crypto.get('label')})")
                    _mark(last_run, "FEAR_GREED_CRYPTO", now)

                # --- PCR (CBOE Total Put/Call) ---
                if _due(last_run, "PCR", now):
                    q_pcr_raw = get_total_pcr()
                    if q_pcr_raw:
                        q_pcr: QuoteWithPct = cast(QuoteWithPct, q_pcr_raw)
                        upsert_value(
                            conn,
                            "PCR",
                            str(q_pcr["symbol"]),
                            float(q_pcr["value"]),
                            str(q_pcr["time"]),
                            extra={"row": q_pcr.get("row")},
                            source_id="cboe",
                            method="scrape",
                        )
                        log.info(f"PCR {q_pcr['value']}")
                    else:
                        log.warning("PCR no disponible (CSV y HTML fallaron).")
                    _mark(last_run, "PCR", now)

                # --- GPR (Global Geopolitical Risk) ---
                if _due(last_run, "GPR", now):
                    q_gpr_raw = get_gpr_global()
                    if q_gpr_raw:
                        q_gpr: QuoteWithPct = cast(QuoteWithPct, q_gpr_raw)
                        upsert_value(
                            conn,
                            "GPR",
                            str(q_gpr["symbol"]),
                            float(q_gpr["value"]),
                            str(q_gpr["time"]),
                            extra={"period": q_gpr.get("period")},
                            source_id="policy_uncertainty",
                            method=(
                                "official_api"
                                if q_gpr.get("source") == "official"
                                else "unofficial_api"
                            ),
                        )
                        log.info(f"GPR {q_gpr['value']} ({q_gpr.get('period')})")
                    else:
                        log.warning("GPR no disponible (CSV candidates fallaron).")
                    _mark(last_run, "GPR", now)

                # --- CREDIT SPREADS (HY/IG OAS) ---
                if _due(last_run, "CREDIT_SPREAD_HY", now):
                    q_hy_raw = get_oas_from_fred("BAMLH0A0HYM2")
                    if q_hy_raw:
                        q_hy: QuoteWithPct = cast(QuoteWithPct, q_hy_raw)
                        upsert_value(
                            conn,
                            "CREDIT_SPREAD_HY",
                            str(q_hy["symbol"]),
                            float(q_hy["value"]),
                            str(q_hy["time"]),
                            extra={"fred_date": q_hy.get("date")},
                            source_id="fred",
                            method="official_api",
                        )
                        log.info(f"HY OAS {q_hy['value']}")
                    _mark(last_run, "CREDIT_SPREAD_HY", now)

                if _due(last_run, "CREDIT_SPREAD_IG", now):
                    q_ig_raw = get_oas_from_fred("BAMLC0A0CM")
                    if q_ig_raw:
                        q_ig: QuoteBasic = cast(QuoteBasic, q_ig_raw)
                        upsert_value(
                            conn,
                            "CREDIT_SPREAD_IG",
                            str(q_ig["symbol"]),
                            float(q_ig["value"]),
                            str(q_ig["time"]),
                            extra={"fred_date": q_ig.get("date")},
                            source_id="fred",
                            method="official_api",
                        )
                        log.info(f"IG OAS {q_ig['value']}")
                    _mark(last_run, "CREDIT_SPREAD_IG", now)

                # --- CALENDAR_BLOCK (ForexFactory) ---
                if _due(last_run, "CALENDAR_BLOCK", now):
                    try:
                        nb_cal_raw = compute_news_block(
                            tz_local=_env_str("LOCAL_TZ", "America/Guayaquil"),
                            lock_high_min=_env_int("NEWS_LOCK_HIGH_MIN", 15),
                            lock_medium_min=_env_int("NEWS_LOCK_MEDIUM_MIN", 10),
                        )
                        nb_cal: QuoteWithPct = cast(QuoteWithPct, nb_cal_raw)
                        upsert_value(
                            conn,
                            feature="CALENDAR_BLOCK",
                            symbol=str(nb_cal["symbol"]),
                            value=float(nb_cal["value"]),
                            ts_iso=str(nb_cal["time"]),
                            extra={
                                "next_event": nb_cal.get("next_event"),
                                "window_min": nb_cal.get("window_min"),
                                "tz": nb_cal.get("tz"),
                            },
                            source_id="forexfactory",
                            method="unofficial_api",
                        )
                        log.info(
                            f"CALENDAR_BLOCK {nb_cal['value']} | next={nb_cal.get('next_event')}"
                        )
                    except Exception as e:
                        log.warning(f"CALENDAR_BLOCK no disponible: {e}")
                    finally:
                        _mark(last_run, "CALENDAR_BLOCK", now)

                # --- 2s10s spread (UST10Y - UST2Y) ---
                if _due(last_run, "SPREAD_2S10S", now):
                    # toma los últimos valores guardados
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT feature, value, ts_utc
                            FROM (
                                SELECT DISTINCT ON (feature) feature, value, ts_utc
                                FROM core.macro_ticks
                                WHERE feature IN ('UST10Y','UST2Y')
                                ORDER BY feature, ts_utc DESC
                            ) q;
                        """
                        )
                        rows = cur.fetchall()  # [(feature, value, ts), ...]
                    vals = {r[0]: float(r[1]) for r in rows}
                    ts = max((r[2] for r in rows), default=_now_utc())
                    if "UST10Y" in vals and "UST2Y" in vals:
                        spread = vals["UST10Y"] - vals["UST2Y"]
                        upsert_value(
                            conn,
                            feature="SPREAD_2S10S",
                            symbol="UST10Y-UST2Y",
                            value=float(spread),
                            ts_iso=str(ts),
                            extra={"unit": "pct"},
                            source_id="derived",
                            method="calc",
                        )
                        log.info(f"SPREAD_2S10S {spread}")
                    _mark(last_run, "SPREAD_2S10S", now)

                # --- HYG/LQD ratio ---
                if _due(last_run, "HYG_LQD_RATIO", now):
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT feature, value, ts_utc
                            FROM (
                                SELECT DISTINCT ON (feature) feature, value, ts_utc
                                FROM core.macro_ticks
                                WHERE feature IN ('HYG','LQD')
                                ORDER BY feature, ts_utc DESC
                            ) q;
                        """
                        )
                        rows = cur.fetchall()
                    vals = {r[0]: float(r[1]) for r in rows}
                    ts = max((r[2] for r in rows), default=_now_utc())
                    if "HYG" in vals and "LQD" in vals and vals["LQD"] != 0:
                        ratio = vals["HYG"] / vals["LQD"]
                        upsert_value(
                            conn,
                            feature="HYG_LQD_RATIO",
                            symbol="HYG/LQD",
                            value=float(ratio),
                            ts_iso=str(ts),
                            extra={},
                            source_id="derived",
                            method="calc",
                        )
                        log.info(f"HYG_LQD_RATIO {ratio}")
                    _mark(last_run, "HYG_LQD_RATIO", now)

                # --- Copper/Gold ratio ---
                if _due(last_run, "COPPER_GOLD_RATIO", now):
                    with conn.cursor() as cur:
                        cur.execute(
                            """
                            SELECT feature, value, ts_utc
                            FROM (
                                SELECT DISTINCT ON (feature) feature, value, ts_utc
                                FROM core.macro_ticks
                                WHERE feature IN ('COPPER','GOLD_FUT')
                                ORDER BY feature, ts_utc DESC
                            ) q;
                        """
                        )
                        rows = cur.fetchall()
                    vals = {r[0]: float(r[1]) for r in rows}
                    ts = max((r[2] for r in rows), default=_now_utc())
                    if "COPPER" in vals and "GOLD_FUT" in vals and vals["GOLD_FUT"] != 0:
                        ratio = vals["COPPER"] / vals["GOLD_FUT"]
                        upsert_value(
                            conn,
                            feature="COPPER_GOLD_RATIO",
                            symbol="HG=F/GC=F",
                            value=float(ratio),
                            ts_iso=str(ts),
                            extra={},
                            source_id="derived",
                            method="calc",
                        )
                        log.info(f"COPPER_GOLD_RATIO {ratio}")
                    _mark(last_run, "COPPER_GOLD_RATIO", now)

                # --- Breadth: % > 200d ---
                if _due(last_run, "BREADTH_SPX_PCT_ABOVE_200D", now) and _yaml_source_is(
                    macro, "BREADTH_SPX_PCT_ABOVE_200D", "Derived"
                ):
                    q = get_breadth_spx_pct_above(200)
                    if q:
                        upsert_value(
                            conn,
                            "BREADTH_SPX_PCT_ABOVE_200D",
                            q["symbol"],
                            float(q["value"]),
                            str(q["time"]),
                            extra={
                                "hits": int(q["hits"]),
                                "total": int(q["total"]),
                                "unit": "percent",
                            },
                            source_id="derived",
                            method="calc",
                        )
                        log.info(f"BREADTH 200d {q['value']:.1f}% ({q['hits']}/{q['total']})")
                    _mark(last_run, "BREADTH_SPX_PCT_ABOVE_200D", now)

                # --- Breadth: % > 50d ---
                if _due(last_run, "BREADTH_SPX_PCT_ABOVE_50D", now) and _yaml_source_is(
                    macro, "BREADTH_SPX_PCT_ABOVE_50D", "Derived"
                ):
                    q = get_breadth_spx_pct_above(50)
                    if q:
                        upsert_value(
                            conn,
                            "BREADTH_SPX_PCT_ABOVE_50D",
                            q["symbol"],
                            float(q["value"]),
                            str(q["time"]),
                            extra={
                                "hits": int(q["hits"]),
                                "total": int(q["total"]),
                                "unit": "percent",
                            },
                            source_id="derived",
                            method="calc",
                        )
                        log.info(f"BREADTH 50d {q['value']:.1f}% ({q['hits']}/{q['total']})")
                    _mark(last_run, "BREADTH_SPX_PCT_ABOVE_50D", now)

                # --- Breadth: advance - decline ---
                if _due(last_run, "BREADTH_SPX_ADV_DEC", now) and _yaml_source_is(
                    macro, "BREADTH_SPX_ADV_DEC", "Derived"
                ):
                    q = get_breadth_spx_adv_dec()
                    if q:
                        upsert_value(
                            conn,
                            "BREADTH_SPX_ADV_DEC",
                            q["symbol"],
                            float(q["value"]),
                            str(q["time"]),
                            extra={
                                "adv": int(q["adv"]),
                                "dec": int(q["dec"]),
                                "unch": int(q["unch"]),
                                "total": int(q["total"]),
                            },
                            source_id="derived",
                            method="calc",
                        )
                        log.info(f"ADV-DEC {q['value']} (A:{q['adv']} D:{q['dec']} U:{q['unch']})")
                    _mark(last_run, "BREADTH_SPX_ADV_DEC", now)

                # --- ETF relative volume (flows-lite proxies) ---
                for feat, sym in [
                    ("RELVOL_SPY_20D", "SPY"),
                    ("RELVOL_QQQ_20D", "QQQ"),
                    ("RELVOL_HYG_20D", "HYG"),
                    ("RELVOL_LQD_20D", "LQD"),
                ]:
                    if _due(last_run, feat, now) and _yaml_source_is(macro, feat, "YahooFinance"):
                        q = get_rel_volume_20d(sym)
                        if q:
                            upsert_value(
                                conn,
                                feat,
                                q["symbol"],
                                float(q["value"]),
                                str(q["time"]),
                                extra={"vol": q["vol"], "avg20": q["avg20"]},
                                source_id="yfinance",
                                method="unofficial_api",
                            )
                            log.info(
                                f"{feat} {q['value']:.2f} (vol {q['vol']:.0f} / avg20 {q['avg20']:.0f})"
                            )
                        _mark(last_run, feat, now)

                # --- CNN Fear & Greed (stocks) ---
                if _due(last_run, "FNG_STOCKS", now):
                    try:
                        f_stocks_raw = fetch_fng()
                        if f_stocks_raw:
                            f_stocks: QuoteBasic = cast(QuoteBasic, f_stocks_raw)
                            upsert_value(
                                conn,
                                feature="FNG_STOCKS",
                                symbol=str(f_stocks["symbol"]),
                                value=float(f_stocks["value"]),
                                ts_iso=str(f_stocks["time"]),
                                extra={"unit": "index_0_100", "via": f_stocks.get("source")},
                                source_id="cnn_fng",
                                method="unofficial_api",
                            )
                            log.info(f"FNG_STOCKS {f_stocks['value']} via {f_stocks.get('source')}")
                        else:
                            log.warning("FNG_STOCKS no disponible.")
                    except Exception as e:
                        log.warning(f"FNG_STOCKS error: {e}")
                    finally:
                        _mark(last_run, "FNG_STOCKS", now)

            except Exception as e:
                log.exception(f"Ciclo error: {e}")
                with suppress(Exception):
                    conn.close()
                time.sleep(2)
                conn = psycopg2.connect(**creds)

            # Latido suave para no quemar CPU
            time.sleep(2)

    finally:
        with suppress(Exception):
            conn.close()


if __name__ == "__main__":
    main()
