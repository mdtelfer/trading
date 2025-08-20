import pandas as pd
import requests
import streamlit as st

st.set_page_config(page_title="Macro Dashboard", layout="wide")
st.title("Macro State (Fused)")

API_URL = st.secrets.get("API_URL", "http://localhost:8055/state/fused/latest")
res = requests.get(API_URL, timeout=5).json()

col1, col2, col3 = st.columns(3)
col1.metric("Risk Multiplier", f"{res.get('risk_multiplier', 1):.2f}")
col2.write("Allowed groups")
col2.write(", ".join(res.get("allowed_groups", [])))
col3.write("Reason")
col3.code(res.get("reason"))

meta = res.get("meta", {})
scen = meta.get("triggered_scenarios", [])
caps = meta.get("group_caps", {})
sugs = meta.get("buy_suggestions", [])

st.subheader("Escenarios activos")
st.write(", ".join(scen) if scen else "—")

# Slots por grupo (puedes pasar la ocupación real desde tu gateway)
st.subheader("Capacity (group caps)")
cap_df = []
for g, cfg in caps.items():
    cap = cfg.get("max_open_positions")
    # TODO: traer ocupación real del gateway; por ahora dummy 0
    open_now = 0
    cap_df.append(
        {
            "group": g,
            "open": open_now,
            "cap": cap,
            "free": (cap - open_now) if cap is not None else None,
        }
    )
st.dataframe(pd.DataFrame(cap_df), use_container_width=True)

st.subheader("Buy suggestions")
if sugs:
    df = pd.DataFrame(sugs)[["symbol", "group", "score", "reasons", "tags"]]
    df["reasons"] = df["reasons"].apply(lambda L: ", ".join(L) if isinstance(L, list) else L)
    df["tags"] = df["tags"].apply(lambda L: ", ".join(L) if isinstance(L, list) else L)
    st.dataframe(df, use_container_width=True)
else:
    st.info("No hay sugerencias activas.")
