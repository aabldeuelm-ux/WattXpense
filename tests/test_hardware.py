"""The hardware contract: device tokens, telemetry, derived figures and relay commands."""

from __future__ import annotations

import pytest

from backend.app.main import app  # noqa: F401  (keeps the app import explicit for TestClient)


def create_device(client, device_id: str = "esp32-1", **overrides) -> dict:
    payload = {
        "id": device_id,
        "name": "Main meter",
        "source": "esp32",
        "sensor_model": "ACS712 30A (assumed family)",
        "sampling_interval_seconds": 60,
        "calibration": {"adc_midpoint": 2048, "mv_per_amp": 66.0},
        "gpio_config": {"current_adc": 34, "relay": 26},
        **overrides,
    }
    response = client.post("/api/devices", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["device_token"].startswith("wxdev_")
    return body


def telemetry(client, token: str, rows: list[dict]):
    return client.post(
        "/api/device/telemetry",
        json={"readings": rows},
        headers={"Authorization": f"Bearer {token}"},
    )


class TestDeviceCredentials:
    def test_telemetry_requires_a_device_token(self, client):
        response = client.post(
            "/api/device/telemetry",
            json={"readings": [{"ts": "2026-09-25T10:00:00Z", "power_w": 100.0}]},
        )
        assert response.status_code == 401

    def test_a_bogus_token_is_refused(self, client):
        response = telemetry(client, "wxdev_not-a-real-token", [{"ts": "2026-09-25T10:00:00Z", "power_w": 100.0}])
        assert response.status_code == 401

    def test_rotating_the_token_invalidates_the_old_one(self, client):
        body = create_device(client, "rotate-me")
        old = body["device_token"]

        rotated = client.post("/api/devices/rotate-me/token")
        assert rotated.status_code == 200
        new = rotated.json()["device_token"]
        assert new != old

        assert telemetry(client, old, [{"ts": "2026-09-25T10:00:00Z", "power_w": 100.0}]).status_code == 401
        assert telemetry(client, new, [{"ts": "2026-09-25T10:00:00Z", "power_w": 100.0}]).status_code == 200

    def test_a_device_token_cannot_be_used_as_a_user_session(self, client, anon_client):
        body = create_device(client, "token-scope")
        response = anon_client.get(
            "/api/analytics/summary",
            headers={"Authorization": f"Bearer {body['device_token']}"},
        )
        assert response.status_code == 401


class TestTelemetryContract:
    def test_voltage_and_current_produce_flagged_estimated_power(self, client):
        body = create_device(client, "vi-meter")
        response = telemetry(
            client,
            body["device_token"],
            [{"ts": "2026-09-25T10:00:00Z", "voltage": 230.0, "current": 0.5}],
        )
        assert response.status_code == 200, response.text
        assert response.json()["inserted"] == 1

        reading = client.get("/api/readings?device_id=vi-meter").json()["readings"][0]
        assert reading["power_w"] == pytest.approx(115.0)
        assert reading["is_simulated"] is False
        assert any("power factor assumed" in flag for flag in reading["quality_flags"])

    def test_power_and_interval_produce_flagged_estimated_energy(self, client):
        body = create_device(client, "power-meter", sampling_interval_seconds=60)
        response = telemetry(
            client,
            body["device_token"],
            [{"ts": "2026-09-25T10:00:00Z", "power_w": 120.0}],
        )
        assert response.status_code == 200, response.text

        reading = client.get("/api/readings?device_id=power-meter").json()["readings"][0]
        assert reading["energy_wh"] == pytest.approx(2.0)
        assert any("energy estimated" in flag for flag in reading["quality_flags"])

    def test_a_current_only_sample_is_refused(self, client):
        body = create_device(client, "current-only")
        response = telemetry(
            client,
            body["device_token"],
            [{"ts": "2026-09-25T10:00:00Z", "current": 0.5}],
        )
        assert response.status_code == 200
        result = response.json()
        assert result["rejected"] == 1
        assert "real power" in result["errors"][0]["reason"]

    def test_a_payload_naming_another_device_is_refused(self, client):
        first = create_device(client, "esp32-a")
        second = create_device(client, "esp32-b")

        response = telemetry(
            client,
            first["device_token"],
            [{"device_id": "esp32-b", "ts": "2026-09-25T10:00:00Z", "power_w": 100.0}],
        )
        assert response.status_code == 200
        result = response.json()
        assert result["inserted"] == 0
        assert result["rejected"] == 1
        assert "does not match" in result["errors"][0]["reason"]
        assert client.get("/api/readings?device_id=esp32-b").json()["total"] == 0

    def test_sensor_status_is_stored_and_flagged(self, client):
        body = create_device(client, "status-meter")
        response = telemetry(
            client,
            body["device_token"],
            [
                {
                    "ts": "2026-09-25T10:00:00Z",
                    "voltage": 230.0,
                    "current": 0.5,
                    "sensor_status": "sensor_fault",
                    "measurement_status": "suspect",
                }
            ],
        )
        assert response.status_code == 200, response.text

        reading = client.get("/api/readings?device_id=status-meter").json()["readings"][0]
        assert reading["sensor_status"] == "sensor_fault"
        assert reading["measurement_status"] == "suspect"
        assert any("sensor reports" in flag for flag in reading["quality_flags"])
        assert any("measurement status" in flag for flag in reading["quality_flags"])

    def test_simulated_firmware_is_labelled_simulated(self, client):
        body = create_device(client, "pretend-meter")
        response = telemetry(
            client,
            body["device_token"],
            [{"ts": "2026-09-25T10:00:00Z", "power_w": 100.0, "simulated": True}],
        )
        assert response.status_code == 200, response.text
        reading = client.get("/api/readings?device_id=pretend-meter").json()["readings"][0]
        assert reading["is_simulated"] is True


class TestHardwareConfiguration:
    def test_calibration_and_gpio_are_stored_per_device(self, client):
        create_device(client, "configured")
        device = client.get("/api/devices").json()["devices"][0]
        assert device["sensor_model"].startswith("ACS712")
        assert device["calibration"] == {"adc_midpoint": 2048, "mv_per_amp": 66.0}
        assert device["gpio_config"] == {"current_adc": 34, "relay": 26}
        assert device["sampling_interval_seconds"] == 60


class TestRelay:
    def test_relay_control_is_off_until_enabled(self, client):
        body = create_device(client, "relay-off")
        assert body["device"]["relay_enabled"] is False

        state = client.get("/api/devices/relay-off/relay").json()
        assert state["relay"]["relay_state"] == "unknown"
        assert state["control_available"] is True

        response = client.post("/api/devices/relay-off/relay", json={"state": "on", "confirm": True})
        assert response.status_code == 409
        assert "not enabled" in response.json()["detail"].lower()

    def test_a_command_is_pending_until_the_device_acknowledges(self, client):
        body = create_device(client, "relay-on", relay_enabled=True)
        token = body["device_token"]

        missing_confirm = client.post("/api/devices/relay-on/relay", json={"state": "on"})
        assert missing_confirm.status_code == 422

        command = client.post(
            "/api/devices/relay-on/relay", json={"state": "on", "confirm": True}
        )
        assert command.status_code == 200, command.text
        assert command.json()["relay"]["relay_state"] == "pending_on"

        # The device polls and sees the pending command, not a claimed physical state.
        poll = client.get("/api/device/relay", headers={"Authorization": f"Bearer {token}"})
        assert poll.status_code == 200
        assert poll.json()["relay"]["relay_state"] == "pending_on"

        ack = client.post(
            "/api/device/relay/ack",
            json={"state": "on", "note": "relay closed"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert ack.status_code == 200, ack.text

        final = client.get("/api/devices/relay-on/relay").json()
        assert final["relay"]["relay_state"] == "on"
        assert final["relay"]["relay_updated_at"] is not None

    def test_relay_endpoints_are_owner_scoped(self, client, second_client):
        create_device(client, "alice-relay", relay_enabled=True)
        assert second_client.get("/api/devices/alice-relay/relay").status_code == 404
        assert (
            second_client.post(
                "/api/devices/alice-relay/relay", json={"state": "on", "confirm": True}
            ).status_code
            == 404
        )
