# scripts/api_state.py
import os

from fastapi import FastAPI
import psycopg2
from psycopg2.extras import RealDictCursor

app = FastAPI(title="Macro State API")


def db_conn():
    return psycopg2.connect(
        host=os.getenv("DB_HOST", "localhost"),
        port=os.getenv("DB_PORT", "5432"),
        dbname=os.getenv("DB_NAME", "trading"),
        user=os.getenv("DB_USER", "postgres"),
        password=os.getenv("DB_PASSWORD", "postgres"),
    )


@app.get("/state/fused/latest")
def fused_latest():
    sql = """
    SELECT ts, tier, risk_multiplier, allowed_groups, prioritize, avoid, reason, meta
    FROM core.macro_state
    WHERE tier='fused'
    ORDER BY ts DESC
    LIMIT 1
    """
    with db_conn() as conn, conn.cursor(cursor_factory=RealDictCursor) as cur:
        cur.execute(sql)
        row = cur.fetchone()
    return row or {}


# Permite ejecutar directo: python scripts/api_state.py
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("scripts.api_state:app", host="127.0.0.1", port=8055, reload=True)
