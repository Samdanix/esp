"""
Library Table Noise Monitor - API
----------------------------------
Receives loudness readings from ESP32 microphone sensors (one per table),
classifies each reading into green / yellow / red, and exposes the current
status per table plus recent history.

Run locally:
    uvicorn app:app --reload --host 0.0.0.0 --port 8000

Deploy on Render.com:
    See render.yaml (start command: uvicorn app:app --host 0.0.0.0 --port $PORT)
"""

import os
import sqlite3
import time
from contextlib import contextmanager
from typing import Optional

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

DB_PATH = os.environ.get("DB_PATH", "noise_monitor.db")

# ---- Thresholds (dB). Tune these to your mic/room calibration. ----
GREEN_MAX = float(os.environ.get("GREEN_MAX", 55))   # <= this is "normal"
YELLOW_MAX = float(os.environ.get("YELLOW_MAX", 70))  # <= this is "medium"; above is "red"

app = FastAPI(title="Library Noise Monitor API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# DB helpers
# ---------------------------------------------------------------------------
@contextmanager
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db():
    with get_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS readings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                table_id TEXT NOT NULL,
                decibel REAL NOT NULL,
                level TEXT NOT NULL,
                ts REAL NOT NULL
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_readings_table_ts ON readings(table_id, ts)"
        )


@app.on_event("startup")
def on_startup():
    init_db()


# ---------------------------------------------------------------------------
# Models
# ---------------------------------------------------------------------------
class SensorReading(BaseModel):
    table_id: str = Field(..., description="Identifier for the table, e.g. 'T1'")
    decibel: float = Field(..., description="Measured loudness in dB")
    device_id: Optional[str] = Field(None, description="Optional ESP32 device id")


class TableStatus(BaseModel):
    table_id: str
    decibel: float
    level: str
    updated_at: float


# ---------------------------------------------------------------------------
# Core classification logic (kept as a pure function -> easy to unit test)
# ---------------------------------------------------------------------------
def classify(decibel: float) -> str:
    if decibel <= GREEN_MAX:
        return "green"
    if decibel <= YELLOW_MAX:
        return "yellow"
    return "red"


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/")
def root():
    return {"status": "ok", "service": "library-noise-monitor-api"}


@app.get("/health")
def health():
    return {"status": "healthy"}


@app.post("/api/sensor/data", response_model=TableStatus)
def post_reading(reading: SensorReading):
    if reading.decibel < 0 or reading.decibel > 160:
        raise HTTPException(status_code=422, detail="decibel out of plausible range (0-160)")

    level = classify(reading.decibel)
    ts = time.time()

    with get_db() as conn:
        conn.execute(
            "INSERT INTO readings (table_id, decibel, level, ts) VALUES (?, ?, ?, ?)",
            (reading.table_id, reading.decibel, level, ts),
        )

    return TableStatus(table_id=reading.table_id, decibel=reading.decibel, level=level, updated_at=ts)


@app.get("/api/tables", response_model=list[TableStatus])
def list_tables():
    with get_db() as conn:
        rows = conn.execute(
            """
            SELECT r.table_id, r.decibel, r.level, r.ts
            FROM readings r
            INNER JOIN (
                SELECT table_id, MAX(ts) AS max_ts
                FROM readings
                GROUP BY table_id
            ) latest ON r.table_id = latest.table_id AND r.ts = latest.max_ts
            ORDER BY r.table_id
            """
        ).fetchall()

    return [
        TableStatus(table_id=row["table_id"], decibel=row["decibel"], level=row["level"], updated_at=row["ts"])
        for row in rows
    ]


@app.get("/api/tables/{table_id}", response_model=TableStatus)
def get_table(table_id: str):
    with get_db() as conn:
        row = conn.execute(
            "SELECT table_id, decibel, level, ts FROM readings WHERE table_id = ? ORDER BY ts DESC LIMIT 1",
            (table_id,),
        ).fetchone()

    if row is None:
        raise HTTPException(status_code=404, detail=f"No readings yet for table '{table_id}'")

    return TableStatus(table_id=row["table_id"], decibel=row["decibel"], level=row["level"], updated_at=row["ts"])


@app.get("/api/tables/{table_id}/history")
def get_table_history(table_id: str, limit: int = 50):
    with get_db() as conn:
        rows = conn.execute(
            "SELECT decibel, level, ts FROM readings WHERE table_id = ? ORDER BY ts DESC LIMIT ?",
            (table_id, limit),
        ).fetchall()

    return [{"decibel": r["decibel"], "level": r["level"], "ts": r["ts"]} for r in rows]


@app.delete("/api/tables/{table_id}")
def clear_table(table_id: str):
    with get_db() as conn:
        cur = conn.execute("DELETE FROM readings WHERE table_id = ?", (table_id,))
    return {"deleted_rows": cur.rowcount}
