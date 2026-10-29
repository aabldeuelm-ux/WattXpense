"""Cross-account isolation.

Every account-facing read and write is scoped to the signed-in user. These tests create two
real accounts on the same database and try to reach across the boundary.
"""

from __future__ import annotations

from tests.conftest import build_readings, post_readings


def _seed(client, device_id: str, watts: float = 200.0):
    return post_readings(client, build_readings(device_id, "2026-09-24T00:00:00Z", days=2, watts=watts))


class TestDeviceIsolation:
    def test_each_account_only_lists_its_own_devices(self, client, second_client):
        _seed(client, "alice-main")
        _seed(second_client, "bob-main")

        alice = client.get("/api/devices").json()
        bob = second_client.get("/api/devices").json()

        assert [device["id"] for device in alice["devices"]] == ["alice-main"]
        assert [device["id"] for device in bob["devices"]] == ["bob-main"]

    def test_a_device_id_cannot_be_taken_over(self, client, second_client):
        _seed(client, "alice-main")
        response = second_client.post(
            "/api/devices",
            json={"id": "alice-main", "name": "Mine now"},
        )
        assert response.status_code == 409
        # The original owner still has it, under the original name.
        devices = client.get("/api/devices").json()["devices"]
        assert devices[0]["id"] == "alice-main"

    def test_readings_of_another_device_are_invisible(self, client, second_client):
        _seed(client, "alice-main")
        assert client.get("/api/readings?device_id=alice-main").json()["total"] > 0

        assert second_client.get("/api/readings?device_id=alice-main").status_code == 404
        assert second_client.delete("/api/readings?device_id=alice-main").status_code == 404

    def test_analytics_ignore_another_accounts_device_id(self, client, second_client):
        _seed(client, "alice-main")

        for path in (
            "/api/analytics/summary?device_id=alice-main",
            "/api/analytics/usage?device_id=alice-main",
            "/api/ai/forecast?device_id=alice-main",
            "/api/ai/anomalies?device_id=alice-main",
            "/api/ai/insights?device_id=alice-main",
        ):
            body = second_client.get(path).json()
            assert body["status"] == "no_device", path

    def test_a_reading_for_another_device_is_rejected_per_sample(self, client, second_client):
        _seed(client, "alice-main")
        before = client.get("/api/readings?device_id=alice-main").json()["total"]

        rows = build_readings("alice-main", "2026-09-25T00:00:00Z", days=1, watts=999.0)
        result = second_client.post(
            "/api/readings", json={"readings": rows, "source": "test"}
        ).json()

        assert result["inserted"] == 0
        assert result["rejected"] == len(rows)
        assert client.get("/api/readings?device_id=alice-main").json()["total"] == before

    def test_manual_reading_for_another_device_is_refused(self, client, second_client):
        _seed(client, "alice-main")
        response = second_client.post(
            "/api/readings/manual",
            json={"device_id": "alice-main", "ts": "2026-09-25T00:00:00Z", "power_w": 500.0},
        )
        assert response.status_code == 404


class TestTariffIsolation:
    def test_tariffs_are_per_account(self, client, second_client):
        alice_tariff = {
            "name": "Alice plan",
            "rate_type": "flat",
            "currency": "INR",
            "flat_rate_per_kwh": 9.5,
        }
        response = client.post("/api/billing/tariff", json=alice_tariff)
        assert response.status_code == 200, response.text

        assert client.get("/api/billing/tariff").json()["tariff"]["name"] == "Alice plan"
        bob_tariff = second_client.get("/api/billing/tariff").json()["tariff"]
        assert bob_tariff["name"] != "Alice plan"
        assert bob_tariff["name"] == "Generic India domestic (example slab tariff)"

        bob_names = [row["name"] for row in second_client.get("/api/billing/tariffs").json()["tariffs"]]
        assert "Alice plan" not in bob_names

    def test_a_new_tariff_deactivates_only_that_accounts_previous_tariff(self, client, second_client):
        client.post(
            "/api/billing/tariff",
            json={"name": "A1", "rate_type": "flat", "currency": "INR", "flat_rate_per_kwh": 5.0},
        )
        second_client.post(
            "/api/billing/tariff",
            json={"name": "B1", "rate_type": "flat", "currency": "INR", "flat_rate_per_kwh": 6.0},
        )
        client.post(
            "/api/billing/tariff",
            json={"name": "A2", "rate_type": "flat", "currency": "INR", "flat_rate_per_kwh": 7.0},
        )

        assert client.get("/api/billing/tariff").json()["tariff"]["name"] == "A2"
        assert second_client.get("/api/billing/tariff").json()["tariff"]["name"] == "B1"


class TestLegacyData:
    def test_unowned_legacy_rows_are_not_visible_to_a_new_account(self, client, second_client):
        # The fixtures created accounts after the schema existed, so there are no legacy
        # rows to see. This asserts the filter itself: an ownerless device is invisible.
        from backend.app.database import SessionLocal
        from backend.app.models import Device

        db = SessionLocal()
        try:
            db.add(Device(id="legacy-row", name="Old meter", owner_id=None, is_simulated=True))
            db.commit()
        finally:
            db.close()

        for account in (client, second_client):
            ids = [device["id"] for device in account.get("/api/devices").json()["devices"]]
            assert "legacy-row" not in ids
            assert account.get("/api/readings?device_id=legacy-row").status_code == 404
