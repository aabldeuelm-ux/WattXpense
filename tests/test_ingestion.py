"""The ingestion layer is the seam where simulated data becomes real data later, so it gets the most tests."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import build_readings, post_readings


def sample(device_id="d1", ts="2026-09-25T00:00:00Z", watts=100.0, voltage=230.0, current=None, energy_wh=None):
    if current is None and voltage:
        current = round(watts / voltage, 6)
    row = {
        "device_id": device_id,
        "ts": ts,
        "voltage": voltage,
        "current": current if current is not None else 0.0,
        "power_w": watts,
        "energy_wh": energy_wh if energy_wh is not None else round(watts / 60.0, 4),
    }
    return row


class TestValidation:
    def test_a_complete_sample_is_accepted(self, client):
        result = post_readings(client, [sample()])
        assert result["inserted"] == 1
        assert result["rejected"] == 0

    def test_missing_device_id_is_rejected(self, client):
        row = sample()
        del row["device_id"]
        response = client.post("/api/readings", json={"readings": [row], "source": "test"})
        assert response.status_code == 422

    def test_negative_power_is_rejected_per_sample(self, client):
        """One impossible reading must not cost us the rest of the batch."""
        result = post_readings(client, [sample(watts=-5.0), sample(ts="2026-09-25T00:01:00Z")])
        assert result["inserted"] == 1
        assert result["rejected"] == 1
        assert "power_w" in result["errors"][0]["reason"]

    def test_negative_energy_is_rejected_per_sample(self, client):
        result = post_readings(client, [sample(energy_wh=-1.0)])
        assert result["rejected"] == 1
        assert "energy_wh" in result["errors"][0]["reason"]

    def test_one_bad_sample_does_not_sink_the_batch(self, client):
        result = post_readings(client, [sample(), sample(watts=-1.0), sample(ts="2026-09-25T00:01:00Z")])
        assert result["received"] == 3
        assert result["inserted"] == 2
        assert result["rejected"] == 1
        assert result["errors"][0]["index"] == 1

    def test_zero_voltage_is_flagged_not_dropped(self, client):
        """0 V is physically implausible, but dropping the sample loses the gap; flag it instead."""
        result = post_readings(client, [sample(voltage=0.0)])
        assert result["inserted"] == 1
        assert result["quality_flag_count"] == 1
        stored = client.get("/api/readings?device_id=d1").json()["readings"][0]
        assert any("0 V" in flag for flag in stored["quality_flags"])

    def test_inconsistent_power_is_flagged_but_kept(self, client):
        """Voltage x current disagreeing with power_w is suspicious, not fatal: keep it and flag it."""
        result = post_readings(client, [sample(voltage=230.0, current=5.0, watts=100.0)])
        assert result["inserted"] == 1
        assert result["quality_flag_count"] == 1
        stored = client.get("/api/readings?device_id=d1").json()["readings"][0]
        assert stored["quality_flags"]
        assert stored["derived_power_factor"] < 0.1


class TestIdempotency:
    def test_resending_identical_data_does_not_duplicate(self, client):
        rows = build_readings("d1", "2026-09-25T00:00:00Z", days=1, watts=100.0)
        first = post_readings(client, rows)
        second = post_readings(client, rows)
        assert first["inserted"] == 24
        assert second["inserted"] == 0
        assert second["duplicates"] == 24
        assert client.get("/api/readings?device_id=d1").json()["total"] == 24

    def test_resending_changed_data_updates_in_place(self, client):
        post_readings(client, [sample(watts=100.0)])
        result = post_readings(client, [sample(watts=250.0)])
        assert result["updated"] == 1
        assert result["inserted"] == 0
        assert client.get("/api/readings?device_id=d1").json()["readings"][0]["power_w"] == pytest.approx(250.0)

    def test_readings_are_scoped_per_device(self, client):
        post_readings(client, [sample(device_id="d1")])
        post_readings(client, [sample(device_id="d2")])
        assert client.get("/api/readings?device_id=d1").json()["total"] == 1
        assert client.get("/api/readings?device_id=d2").json()["total"] == 1


class TestBatchEfficiency:
    def test_a_device_is_registered_once_per_batch(self, client):
        """A batch of many samples for one device must not queue a device INSERT per sample."""
        rows = build_readings("d1", "2026-09-25T00:00:00Z", days=2, watts=100.0)
        result = post_readings(client, rows)
        assert result["inserted"] == 48
        assert result["devices_registered"] == ["d1"]
        assert result["devices_touched"] == ["d1"]

    def test_several_devices_in_one_batch(self, client):
        rows = build_readings("d1", "2026-09-25T00:00:00Z", days=1, watts=100.0)
        rows += build_readings("d2", "2026-09-25T00:00:00Z", days=1, watts=200.0)
        result = post_readings(client, rows)
        assert result["inserted"] == 48
        assert sorted(result["devices_registered"]) == ["d1", "d2"]
        assert sorted(result["devices_touched"]) == ["d1", "d2"]


class TestDeviceRegistration:
    def test_first_device_becomes_primary(self, client):
        post_readings(client, [sample(device_id="first")])
        devices = client.get("/api/devices").json()["devices"]
        assert devices[0]["id"] == "first"
        assert devices[0]["is_primary"] is True

    def test_second_device_is_not_primary(self, client):
        post_readings(client, [sample(device_id="first")])
        post_readings(client, [sample(device_id="second")])
        devices = {device["id"]: device for device in client.get("/api/devices").json()["devices"]}
        assert devices["first"]["is_primary"] is True
        assert devices["second"]["is_primary"] is False

    def test_reporting_interval_is_derived_from_the_data(self, client):
        post_readings(client, build_readings("d1", "2026-09-25T00:00:00Z", days=2, watts=100.0, interval_minutes=15))
        device = client.get("/api/devices").json()["devices"][0]
        assert device["nominal_interval_seconds"] == 900


class TestSimulatedLabelling:
    def test_simulator_data_is_flagged_simulated(self, client):
        post_readings(client, [sample()], source="simulator")
        payload = client.get("/api/readings?device_id=d1").json()
        assert payload["readings"][0]["is_simulated"] is True
        assert payload["data_label"] == "Simulated data"

    def test_real_data_is_not_flagged_simulated(self, client):
        post_readings(client, [sample()], source="esp32")
        payload = client.get("/api/readings?device_id=d1").json()
        assert payload["readings"][0]["is_simulated"] is False
        assert payload["data_label"] == "Measured data"

    def test_mixing_sources_reports_the_simulated_share(self, client):
        post_readings(client, [sample(ts="2026-09-25T00:00:00Z")], source="simulator")
        post_readings(client, [sample(ts="2026-09-25T00:01:00Z")], source="esp32")
        device = client.get("/api/devices").json()["devices"][0]
        assert device["simulated_reading_share"] == pytest.approx(0.5)


class TestManualEntry:
    def test_power_only_entry_fills_in_the_rest(self, client):
        post_readings(client, build_readings("d1", "2026-09-25T00:00:00Z", days=1, watts=100.0))
        response = client.post(
            "/api/readings/manual",
            json={"device_id": "d1", "ts": "2026-09-26T00:00:00Z", "power_w": 600.0},
        )
        assert response.status_code == 200, response.text
        # 600 W over the device's usual 60 min interval = 600 Wh
        assert response.json()["derived"]["energy_wh"] == pytest.approx(600.0)

    def test_voltage_and_current_are_enough(self, client):
        post_readings(client, build_readings("d1", "2026-09-25T00:00:00Z", days=1, watts=100.0))
        response = client.post(
            "/api/readings/manual",
            json={"device_id": "d1", "ts": "2026-09-26T00:00:00Z", "voltage": 230.0, "current": 2.0},
        )
        assert response.status_code == 200, response.text
        assert response.json()["derived"]["energy_wh"] == pytest.approx(460.0)

    def test_entry_without_any_measurement_is_refused(self, client):
        post_readings(client, [sample()])
        response = client.post("/api/readings/manual", json={"device_id": "d1", "ts": "2026-09-26T00:00:00Z"})
        assert response.status_code == 422

    def test_entry_for_an_unknown_device_is_refused(self, client):
        response = client.post(
            "/api/readings/manual", json={"device_id": "ghost", "ts": "2026-09-26T00:00:00Z", "power_w": 10.0}
        )
        assert response.status_code == 404

    def test_unexpected_field_is_refused(self, client):
        post_readings(client, [sample()])
        response = client.post(
            "/api/readings/manual",
            json={"device_id": "d1", "ts": "2026-09-26T00:00:00Z", "power_w": 10.0, "watts": 10.0},
        )
        assert response.status_code == 422


class TestTimezoneHandling:
    def test_offset_timestamps_are_stored_as_utc(self, client):
        post_readings(client, [sample(ts="2026-09-25T12:00:00+02:00")])
        stored = client.get("/api/readings?device_id=d1").json()["readings"][0]["ts"]
        assert stored.startswith("2026-09-25T10:00:00")

    def test_naive_timestamp_is_refused(self, client):
        response = client.post(
            "/api/readings", json={"readings": [{**sample(), "ts": "2026-09-25T10:00:00"}], "source": "test"}
        )
        assert response.status_code == 422

    def test_naive_timestamp_is_refused_by_manual_entry_too(self, client):
        post_readings(client, [sample()])
        response = client.post(
            "/api/readings/manual", json={"device_id": "d1", "ts": "2026-09-26T00:00:00", "power_w": 10.0}
        )
        assert response.status_code == 422

    def test_the_same_instant_sent_two_ways_collapses_to_one_row(self, client):
        post_readings(client, [sample(ts="2026-09-25T10:00:00Z")])
        result = post_readings(client, [sample(ts="2026-09-25T12:00:00+02:00")])
        assert result["duplicates"] == 1
        assert client.get("/api/readings?device_id=d1").json()["total"] == 1


class TestCsvImport:
    def test_csv_rows_go_through_the_same_path(self, client, tmp_path):
        path = tmp_path / "readings.csv"
        lines = ["ts,voltage,current,power_w,energy_wh"]
        base = dt.datetime(2026, 9, 25, tzinfo=dt.timezone.utc)
        for step in range(24):
            when = (base + dt.timedelta(hours=step)).isoformat().replace("+00:00", "Z")
            lines.append(f"{when},230.0,0.5,115.0,115.0")
        path.write_text("\n".join(lines))

        response = client.post(f"/api/readings/import-csv?path={path}&device_id=csv-1&source=csv")
        assert response.status_code == 200, response.text
        assert response.json()["inserted"] == 24
        assert client.get("/api/readings?device_id=csv-1").json()["total"] == 24

    def test_missing_file_is_a_clean_error(self, client):
        response = client.post("/api/readings/import-csv?path=/nope/missing.csv")
        assert response.status_code == 400
