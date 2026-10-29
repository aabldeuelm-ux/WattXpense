from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

# The database engine is built at import time, so the environment has to be set
# before anything from backend.app is imported. pytest loads conftest first,
# which makes this the right place to point tests at a throwaway database.
_TMP_DIR = tempfile.mkdtemp(prefix="wattxpense-tests-")
os.environ["WATTXPENSE_DATABASE_URL"] = f"sqlite:///{Path(_TMP_DIR) / 'test.db'}"
os.environ["WATTXPENSE_TIMEZONE"] = "UTC"
# Pin "now" so day boundaries, billing periods and forecasts are deterministic.
os.environ["WATTXPENSE_SIMULATED_NOW"] = "2026-09-26T05:00:00Z"
# Relay control is off by default in a real install. The hardware tests exercise the
# explicit opt-in path, so enable the install-wide switch for the test process only.
os.environ["WATTXPENSE_RELAY_CONTROL_ENABLED"] = "true"

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.auth import reset_rate_limits  # noqa: E402
from backend.app.database import Base, engine  # noqa: E402
from backend.app.main import app  # noqa: E402

TEST_EMAIL = "owner@example.test"
TEST_PASSWORD = "correct horse battery staple"


def register_and_login(client: TestClient, email: str = TEST_EMAIL, password: str = TEST_PASSWORD) -> dict:
    """Create an account and install its CSRF header on the test client."""
    response = client.post(
        "/api/auth/register",
        json={"email": email, "password": password, "display_name": "Test owner"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    client.headers.update({"X-CSRF-Token": body["csrf_token"]})
    return body


def login(client: TestClient, email: str = TEST_EMAIL, password: str = TEST_PASSWORD) -> dict:
    response = client.post("/api/auth/login", json={"email": email, "password": password})
    assert response.status_code == 200, response.text
    body = response.json()
    client.headers.update({"X-CSRF-Token": body["csrf_token"]})
    return body


def _new_client() -> TestClient:
    Base.metadata.create_all(bind=engine)
    reset_rate_limits()
    return TestClient(app)


@pytest.fixture()
def anon_client():
    """A TestClient on an empty database with no signed-in account."""
    client = _new_client()
    try:
        yield client
    finally:
        client.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def client():
    """An authenticated TestClient on an empty database.

    Recreated per test so tests cannot leak state into each other. The default account is
    deliberately separate from the data: tests that want a second user create one through
    the registration endpoint.
    """
    client = _new_client()
    try:
        register_and_login(client)
        yield client
    finally:
        client.close()
        Base.metadata.drop_all(bind=engine)


@pytest.fixture()
def second_client():
    """A second, independent account on the same database. Used for isolation tests."""
    client = _new_client()
    try:
        register_and_login(client, "second@example.test", "second correct horse battery")
        yield client
    finally:
        client.close()


@pytest.fixture()
def seeded_client(client):
    """A client with a small deterministic history pushed through the real ingestion endpoint."""
    post_readings(client, build_readings("sim-main-01", "2026-09-20T00:00:00Z", days=3, watts=250.0))
    return client


def build_readings(
    device_id: str,
    start: str,
    days: int = 3,
    watts: float = 250.0,
    interval_minutes: int = 60,
    voltage: float = 230.0,
    hours: int | None = None,
):
    """A flat, known load. Because the power is constant the expected energy is easy to assert exactly.

    Pass `hours` instead of `days` to produce a deliberately partial window.
    """
    import datetime as dt

    begin = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    total_minutes = hours * 60 if hours is not None else days * 24 * 60
    rows = []
    for step in range(total_minutes // interval_minutes):
        ts = begin + dt.timedelta(minutes=interval_minutes * step)
        hours_per_sample = interval_minutes / 60.0
        rows.append(
            {
                "device_id": device_id,
                "ts": ts.isoformat().replace("+00:00", "Z"),
                "voltage": voltage,
                "current": round(watts / voltage, 6),
                "power_w": watts,
                "energy_wh": round(watts * hours_per_sample, 4),
            }
        )
    return rows


def post_readings(client, rows, source="test"):
    response = client.post("/api/readings", json={"readings": rows, "source": source})
    assert response.status_code == 200, response.text
    return response.json()


def set_now(monkeypatch, iso: str) -> None:
    """Move the pinned clock so a test can place itself in a different billing month."""
    monkeypatch.setenv("WATTXPENSE_SIMULATED_NOW", iso)
