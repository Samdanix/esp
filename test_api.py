"""
Test suite for the Library Noise Monitor API.

Usage:
  1) Local / in-process tests (no server needed, uses FastAPI TestClient):
       pytest test_api.py -v

  2) Test cases against a REAL running server (local or Render deployment):
       BASE_URL="https://your-app.onrender.com" pytest test_api.py -v --live

     or run it directly as a script:
       BASE_URL="https://your-app.onrender.com" python test_api.py
"""

import os
import sys
import time
import uuid

import pytest
import httpx

BASE_URL = os.environ.get("BASE_URL")  # if set, tests hit this URL over HTTP
LIVE = "--live" in sys.argv or BASE_URL is not None


# ---------------------------------------------------------------------------
# Client fixture: either FastAPI's in-process TestClient, or a real HTTP client
# ---------------------------------------------------------------------------
@pytest.fixture(scope="session")
def client():
    if LIVE:
        url = BASE_URL or "http://localhost:8000"
        with httpx.Client(base_url=url, timeout=10.0) as c:
            yield c
    else:
        from fastapi.testclient import TestClient
        from app import app, init_db

        init_db()
        with TestClient(app) as c:
            yield c


@pytest.fixture()
def table_id():
    # unique id per test so tests don't collide with each other's data
    return f"T-{uuid.uuid4().hex[:8]}"


# ---------------------------------------------------------------------------
# Unit tests for the pure classification function (green/yellow/red)
# ---------------------------------------------------------------------------
def test_classify_green():
    from app import classify
    assert classify(30) == "green"
    assert classify(55) == "green"  # boundary, inclusive


def test_classify_yellow():
    from app import classify
    assert classify(56) == "yellow"
    assert classify(70) == "yellow"  # boundary, inclusive


def test_classify_red():
    from app import classify
    assert classify(71) == "red"
    assert classify(120) == "red"


# ---------------------------------------------------------------------------
# API integration tests
# ---------------------------------------------------------------------------
def test_health_check(client):
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "healthy"


def test_post_reading_green(client, table_id):
    resp = client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 40})
    assert resp.status_code == 200
    body = resp.json()
    assert body["table_id"] == table_id
    assert body["decibel"] == 40
    assert body["level"] == "green"
    assert "updated_at" in body


def test_post_reading_yellow(client, table_id):
    resp = client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 60})
    assert resp.status_code == 200
    assert resp.json()["level"] == "yellow"


def test_post_reading_red(client, table_id):
    resp = client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 85})
    assert resp.status_code == 200
    assert resp.json()["level"] == "red"


def test_post_reading_with_device_id(client, table_id):
    resp = client.post(
        "/api/sensor/data",
        json={"table_id": table_id, "decibel": 45, "device_id": "esp32-A1"},
    )
    assert resp.status_code == 200
    assert resp.json()["level"] == "green"


def test_post_reading_rejects_out_of_range_negative(client, table_id):
    resp = client.post("/api/sensor/data", json={"table_id": table_id, "decibel": -5})
    assert resp.status_code == 422


def test_post_reading_rejects_out_of_range_too_high(client, table_id):
    resp = client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 200})
    assert resp.status_code == 422


def test_post_reading_missing_fields(client, table_id):
    resp = client.post("/api/sensor/data", json={"table_id": table_id})
    assert resp.status_code == 422  # decibel is required


def test_get_table_status_after_post(client, table_id):
    client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 65})
    resp = client.get(f"/api/tables/{table_id}")
    assert resp.status_code == 200
    body = resp.json()
    assert body["table_id"] == table_id
    assert body["level"] == "yellow"


def test_get_table_status_returns_latest_reading(client, table_id):
    client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 40})
    time.sleep(0.05)
    client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 90})  # most recent
    resp = client.get(f"/api/tables/{table_id}")
    assert resp.status_code == 200
    assert resp.json()["level"] == "red"
    assert resp.json()["decibel"] == 90


def test_get_unknown_table_returns_404(client):
    resp = client.get("/api/tables/does-not-exist-xyz")
    assert resp.status_code == 404


def test_list_tables_includes_posted_table(client, table_id):
    client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 50})
    resp = client.get("/api/tables")
    assert resp.status_code == 200
    ids = [t["table_id"] for t in resp.json()]
    assert table_id in ids


def test_table_history_returns_readings_desc(client, table_id):
    client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 30})
    time.sleep(0.05)
    client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 80})
    resp = client.get(f"/api/tables/{table_id}/history")
    assert resp.status_code == 200
    history = resp.json()
    assert len(history) >= 2
    # most recent first
    assert history[0]["decibel"] == 80


def test_delete_table_clears_readings(client, table_id):
    client.post("/api/sensor/data", json={"table_id": table_id, "decibel": 40})
    del_resp = client.delete(f"/api/tables/{table_id}")
    assert del_resp.status_code == 200
    assert del_resp.json()["deleted_rows"] >= 1

    get_resp = client.get(f"/api/tables/{table_id}")
    assert get_resp.status_code == 404


# ---------------------------------------------------------------------------
# Allow `python test_api.py` to run as a quick smoke test against BASE_URL
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
