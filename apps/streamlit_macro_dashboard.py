from __future__ import annotations

from datetime import datetime
import os
import time as _t
from typing import Any

import pandas as pd
import psycopg2
from psycopg2.extras import RealDictCursor
import streamlit as st

# =============== Configuración básica ===============
st.set_page_config(
    page_title="Macro Dashboard (Intraday)",
    page_icon="📈",
    layout="wide",
)

# Sidebar: conexión
st.sidebar.header("🔌 Conexión DB")
DB_HOST = st.sidebar.text_input("DB_HOST", os.getenv("DB_HOST", "localhost"))
DB_PORT = st.sidebar.number_input("DB_PORT", value=int(os.getenv("DB_PORT", "5432")))
DB_NAME = st.sidebar.text_input("DB_NAME", os.getenv("DB_NAME", "trading"))
DB_USER = st.sidebar.text_input("DB_USER", os.getenv("DB_USER", "postgres"))
DB_PASSWORD = st.sidebar.text_input(
    "DB_PASSWORD", os.getenv("DB_PASSWORD", "postgres"), type="password"
)
refresh_sec = st.sidebar.slider("Auto‑refresh (s)", 5, 120, 15)


@st.cache_data(ttl=5)
def _connect_params(host, port, db, user, pwd):
    # función cacheada para evitar re-render excesivo de inputs
    return dict(host=host, port=port, dbname=db, user=user, password=pwd)


conn_kwargs = _connect_params(DB_HOST, DB_PORT, DB_NAME, DB_USER, DB_PASSWORD)

# =============== Helpers DB ===============


def _pg_conn():
    return psycopg2.connect(**conn_kwargs, cursor_factory=RealDictCursor)


@st.cache_data(ttl=5)
def fetch_macro_dashboard() -> dict[str, Any]:
    sql = """
    WITH fused AS (
      SELECT *
      FROM core.macro_state
      WHERE tier='fused'
      ORDER BY ts DESC
      LIMIT 1
    )
    SELECT
      ts,
      risk_multiplier,
      meta ->> 'risk_band'                 AS risk_band,
      COALESCE((meta ->> 'can_open_new')::boolean, false) AS can_open_new,
      allowed_groups,
      prioritize,
      avoid,
      meta -> 'triggered_scenarios'        AS triggered_scenarios,
      meta -> 'hard_blocks'                AS hard_blocks,
      meta -> 'buy_suggestions'            AS buy_suggestions
    FROM fused;
    """
    with _pg_conn() as conn, conn.cursor() as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row or {}


@st.cache_data(ttl=5)
def fetch_fast_slow() -> pd.DataFrame:
    sql = """
    SELECT
      tier,
      ts,
      risk_multiplier,
      meta ->> 'risk_band'                      AS risk_band,
      COALESCE((meta ->> 'can_open_new')::boolean, false) AS can_open_new,
      array_to_string(allowed_groups, ', ')     AS allowed_groups,
      array_to_string(prioritize,     ', ')     AS prioritize,
      array_to_string(avoid,          ', ')     AS avoid,
      COALESCE(string_agg(x.sc::text, ', ') FILTER (WHERE x.sc IS NOT NULL), '') AS triggered_scenarios,
      COALESCE(string_agg(hb.hb::text, ', ') FILTER (WHERE hb.hb IS NOT NULL), '') AS hard_blocks
    FROM core.macro_state_latest
    LEFT JOIN LATERAL (
        SELECT jsonb_array_elements_text(meta->'triggered_scenarios') AS sc
    ) x ON true
    LEFT JOIN LATERAL (
        SELECT jsonb_array_elements_text(meta->'hard_blocks') AS hb
    ) hb ON true
    WHERE tier IN ('fast','slow')
    GROUP BY tier, ts, risk_multiplier, risk_band, can_open_new, allowed_groups, prioritize, avoid
    ORDER BY tier;
    """
    with _pg_conn() as conn:
        df = pd.read_sql(sql, conn)
    return df


@st.cache_data(ttl=5)
def fetch_features_latest() -> pd.DataFrame:
    sql = """
    SELECT
      feature,
      value,
      ts,
      status,
      aux_values ->> 'symbol' AS symbol,
      CASE
        WHEN aux_values ? 'value_pct'
          THEN (aux_values ->> 'value_pct')::numeric
        WHEN (aux_values -> 'unit')::text = '{"stored": "decimal", "original": "percent"}'
          THEN (value * 100)::numeric
        ELSE NULL
      END AS value_pct
    FROM core.v_macro_latest
    ORDER BY feature;
    """
    with _pg_conn() as conn:
        df = pd.read_sql(sql, conn)
    return df


# =============== UI ===============

st.title("📊 Macro Dashboard — Intradía Long‑Only")
st.caption(datetime.utcnow().strftime("UTC %Y-%m-%d %H:%M:%S"))

# --- Bloque principal: FUSED ---
row = fetch_macro_dashboard()
if not row:
    st.warning("No hay estado 'fused' aún. Corre el evaluator para poblar core.macro_state.")
    st.stop()

col1, col2, col3, col4 = st.columns([1, 1, 1, 2])
with col1:
    st.metric("Risk Band", row.get("risk_band", "unknown"))
with col2:
    st.metric("Risk Multiplier", f"{float(row.get('risk_multiplier', 0)):.2f}")
with col3:
    st.metric("Can Open New", "✅" if row.get("can_open_new", False) else "⛔️")
with col4:
    hb = row.get("hard_blocks") or []
    sc = row.get("triggered_scenarios") or []
    if hb:
        st.write("**Hard Blocks**:")
        st.write(", ".join([str(x) for x in hb]))
    if sc:
        st.write("**Scenarios**:")
        st.write(", ".join([str(x) for x in sc]))

st.markdown("---")

# --- Sugerencias de compra ---
st.subheader("🎯 Sugerencias (fused)")
sugs: list[dict[str, Any]] = row.get("buy_suggestions") or []
if sugs:
    sug_df = pd.DataFrame(sugs)
    if "score" in sug_df.columns:
        sug_df = sug_df.sort_values("score", ascending=False)
    rename = {
        "symbol": "Símbolo",
        "group": "Grupo",
        "score": "Score",
        "reasons": "Escenarios",
        "tags": "Tags",
    }
    st.dataframe(sug_df.rename(columns=rename), use_container_width=True, hide_index=True)
else:
    st.info("Sin sugerencias todavía para el estado actual.")

st.markdown("---")

# --- Fast / Slow comparativa ---
st.subheader("⏱️ Fast vs Slow (últimos)")
df_fs = fetch_fast_slow()
if not df_fs.empty:
    st.dataframe(df_fs, use_container_width=True, hide_index=True)
else:
    st.info("Sin estados fast/slow.")

st.markdown("---")

# --- Últimos features (snapshot) ---
st.subheader("🧩 Últimos Features — Snapshot")
df_feat = fetch_features_latest()
if not df_feat.empty:
    df_feat = df_feat[["feature", "value", "value_pct", "symbol", "status", "ts"]]
    st.dataframe(df_feat, use_container_width=True, hide_index=True)
else:
    st.info("No hay features en core.v_macro_latest.")

# =============== Auto‑refresh sencillo y fiable ===============
st.caption("Auto‑refresh activo")
if refresh_sec and refresh_sec > 0:
    _t.sleep(int(refresh_sec))
    # tocar query params (fuerza un re-run y mantiene navegación)
    try:
        st.query_params.update({"_": str(datetime.utcnow().timestamp())})
    except Exception:
        # fallback en instalaciones viejas
        st.experimental_set_query_params(_=datetime.utcnow().timestamp())
    st.experimental_rerun()
