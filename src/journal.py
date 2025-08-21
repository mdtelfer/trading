# ──────────────────────────────────────────────────────────────────────────────
# src/journal.py
# ──────────────────────────────────────────────────────────────────────────────
from __future__ import annotations

import sqlite3
from typing import Any

DDL = """
CREATE TABLE IF NOT EXISTS tech_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    symbol TEXT,
    tf TEXT,
    event TEXT,
    session TEXT,
    level REAL,
    features_json TEXT,
    structure_json TEXT
);
CREATE TABLE IF NOT EXISTS signals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT,
    symbol TEXT,
    setup_id TEXT,
    side TEXT,
    entry TEXT,
    sl TEXT,
    tp1_rr REAL,
    confidence TEXT,
    risk_group TEXT,
    risk_R REAL,
    session TEXT,
    meta_json TEXT
);
"""


class Journal:
    def __init__(self, path: str = "tech_journal.sqlite"):
        self.conn = sqlite3.connect(path)
        self.conn.executescript(DDL)
        self.conn.commit()

    def log_event(self, ev: dict[str, Any]):
        self.conn.execute(
            "INSERT INTO tech_events(ts, symbol, tf, event, session, level, features_json, structure_json) VALUES (?,?,?,?,?,?,?,?)",
            (
                ev["ts"],
                ev["symbol"],
                ev["tf"],
                ev["event"],
                ev["session"],
                ev["level"],
                str(ev.get("features", {})),
                str(ev.get("structure", {})),
            ),
        )
        self.conn.commit()

    def log_signal(self, sig: dict[str, Any]):
        self.conn.execute(
            "INSERT INTO signals(ts, symbol, setup_id, side, entry, sl, tp1_rr, confidence, risk_group, risk_R, session, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                sig["ts"],
                sig["symbol"],
                sig["setup_id"],
                sig["side"],
                str(sig["entry"]),
                str(sig["sl"]),
                sig["tp1_rr"],
                sig["confidence"],
                sig["risk_group"],
                sig["risk_R"],
                sig["session"],
                str(sig.get("meta", {})),
            ),
        )
        self.conn.commit()
