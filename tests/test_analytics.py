"""Aggregation tests use a flat known load, so every expected number can be worked out by hand."""

from __future__ import annotations

import pytest

from tests.conftest import build_readings, post_readings, set_now


class TestEnergyMaths:
    def test_constant_load_gives_the_expected_daily_total(self, client):
        # 1 kW for 3 days = 72 kWh
        post_readings(client, build_readings("d1", "2026-09-22T00:00:00Z", days=3, watts=1000.0))
        daily = client.get("/api/analytics/usage?view=month").json()["daily"]
        totals = {row["date"]: row["energy_kwh"] for row in daily}
        assert totals["2026-09-22"] == pytest.approx(24.0)
        assert totals["2026-09-23"] == pytest.approx(24.0)
        assert totals["2026-09-24"] == pytest.approx(24.0)

    def test_daily_average_matches_the_daily_totals(self, client):
        post_readings(client, build_readings("d1", "2026-09-22T00:00:00Z", days=3, watts=500.0))
        usage = client.get("/api/analytics/usage?view=month").json()
        assert usage["totals"]["energy_kwh"] == pytest.approx(36.0)
        assert usage["totals"]["reading_count"] == 72

    def test_energy_is_measured_in_wh_not_a_cumulative_total(self, client, monkeypatch):
        """The biggest trap in meter data: energy_wh is per interval, so it must be summed."""
        set_now(monkeypatch, "2026-09-25T23:00:00Z")
        post_readings(client, build_readings("d1", "2026-09-25T00:00:00Z", days=1, watts=100.0))
        usage = client.get("/api/analytics/usage?view=day").json()
        # 24 samples x 100 Wh = 2400 Wh = 2.4 kWh, not 2400 kWh.
        assert usage["totals"]["energy_kwh"] == pytest.approx(2.4)

    def test_power_factor_is_derived_from_voltage_and_current(self, client):
        post_readings(client, [r for r in build_readings("d1", "2026-09-25T00:00:00Z", days=1, watts=100.0)])
        row = client.get("/api/readings?device_id=d1").json()["readings"][0]
        assert row["apparent_power_va"] == pytest.approx(100.0, abs=0.5)
        assert row["derived_power_factor"] == pytest.approx(1.0, abs=0.02)


class TestCoverage:
    def test_a_complete_day_reads_as_full_coverage(self, client):
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", days=1, watts=100.0))
        row = next(r for r in client.get("/api/analytics/usage?view=month").json()["daily"] if r["date"] == "2026-09-24")
        assert row["coverage_ratio"] == pytest.approx(1.0)

    def test_widely_spaced_samples_still_cover_the_whole_day(self, client):
        """12 samples two hours apart tile all 24 h, so the day really is fully covered."""
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", hours=24, watts=100.0, interval_minutes=120))
        row = next(r for r in client.get("/api/analytics/usage?view=month").json()["daily"] if r["date"] == "2026-09-24")
        assert row["coverage_ratio"] == pytest.approx(1.0)

    def test_a_six_hour_window_reads_as_partial_coverage(self, client):
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", hours=6, watts=100.0))
        row = next(r for r in client.get("/api/analytics/usage?view=month").json()["daily"] if r["date"] == "2026-09-24")
        assert row["coverage_ratio"] == pytest.approx(0.25, abs=0.02)

    def test_partial_today_is_not_compared_with_a_full_yesterday(self, client, monkeypatch):
        """A -97% change would be nonsense and actively misleading, so it must be suppressed."""
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", days=1, watts=1000.0))
        set_now(monkeypatch, "2026-09-25T06:00:00Z")
        post_readings(client, build_readings("d1", "2026-09-25T00:00:00Z", hours=6, watts=1000.0))
        today = client.get("/api/analytics/summary").json()["energy_today"]
        assert today["coverage_ratio"] < 0.8
        assert today["change_vs_yesterday_percent"] is None
        assert "not compared" in today["change_note"]

    def test_an_undercovered_comparison_window_is_reported(self, client):
        # Only today has data, so the previous window has nothing to compare against.
        post_readings(client, build_readings("d1", "2026-09-25T00:00:00Z", days=1, watts=100.0))
        totals = client.get("/api/analytics/usage?view=month").json()["totals"]
        assert totals["previous_coverage_ratio"] < 0.8
        assert totals["change_percent"] is None
        assert totals["comparison_note"]


class TestViews:
    @pytest.fixture(autouse=True)
    def _data(self, client):
        post_readings(client, build_readings("d1", "2026-09-20T00:00:00Z", days=6, watts=300.0))

    def test_day_view_buckets_by_hour(self, client):
        usage = client.get("/api/analytics/usage?view=day").json()
        assert usage["bucket"] == "Hour"
        assert usage["range"]["days"] == 1

    def test_week_and_month_bucket_by_day(self, client):
        assert client.get("/api/analytics/usage?view=week").json()["bucket"] == "Day"
        assert client.get("/api/analytics/usage?view=month").json()["bucket"] == "Day"

    def test_an_unknown_view_is_a_clear_error(self, client):
        response = client.get("/api/analytics/usage?view=fortnight")
        assert response.status_code == 400
        assert "day, week, month" in response.json()["detail"]

    def test_buckets_and_daily_rows_line_up(self, client):
        usage = client.get("/api/analytics/usage?view=week").json()
        assert len(usage["buckets"]) == len(usage["daily"])

    def test_bucket_totals_add_up_to_the_range_total(self, client):
        usage = client.get("/api/analytics/usage?view=week").json()
        assert sum(bucket["energy_kwh"] for bucket in usage["buckets"]) == pytest.approx(
            usage["totals"]["energy_kwh"], abs=0.01
        )

    def test_the_average_profile_covers_all_24_hours(self, client):
        profile = client.get("/api/analytics/usage?view=week").json()["average_profile"]
        assert [row["hour"] for row in profile] == list(range(24))

    def test_busiest_day_is_the_highest_daily_total(self, client):
        usage = client.get("/api/analytics/usage?view=week").json()
        best = max(usage["daily"], key=lambda row: row["energy_kwh"])
        assert usage["peaks"]["busiest_day"] == best["date"]
        assert usage["peaks"]["busiest_day_kwh"] == pytest.approx(best["energy_kwh"])


class TestNightSplit:
    def test_night_energy_is_reported_separately(self, client):
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", days=2, watts=200.0))
        daily = client.get("/api/analytics/usage?view=month").json()["daily"]
        row = next(r for r in daily if r["date"] == "2026-09-24")
        # 7 of 24 hours are 23:00-06:00
        assert row["night_kwh"] == pytest.approx(row["energy_kwh"] * 7 / 24, abs=0.05)
        assert row["night_kwh"] < row["energy_kwh"]


class TestSummary:
    def test_summary_reports_the_latest_reading(self, client, monkeypatch):
        # Last sample is 11:00, so with the clock at 11:10 it is 10 minutes old and still "live".
        set_now(monkeypatch, "2026-09-25T11:10:00Z")
        post_readings(client, build_readings("d1", "2026-09-25T00:00:00Z", hours=12, watts=150.0))
        power = client.get("/api/analytics/summary").json()["current_power"]
        assert power["latest_w"] == pytest.approx(150.0)
        assert power["age_minutes"] == pytest.approx(10.0, abs=1.0)
        assert power["is_stale"] is False
        assert power["timestamp_in_future"] is False

    def test_a_clock_ahead_does_not_look_like_a_live_reading(self, client, monkeypatch):
        """A device reporting a future timestamp must not be shown as the freshest possible data."""
        set_now(monkeypatch, "2026-09-25T12:00:00Z")
        post_readings(client, build_readings("d1", "2026-09-26T00:00:00Z", hours=3, watts=150.0))
        power = client.get("/api/analytics/summary").json()["current_power"]
        assert power["timestamp_in_future"] is True
        assert power["is_stale"] is True
        assert "clock" in power["clock_note"].lower()

    def test_stale_readings_are_called_out(self, client, monkeypatch):
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", days=1, watts=150.0))
        set_now(monkeypatch, "2026-09-26T00:00:00Z")
        power = client.get("/api/analytics/summary").json()["current_power"]
        assert power["is_stale"] is True
        assert power["age_minutes"] > 1000

    def test_summary_proclaims_when_everything_is_simulated(self, client):
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", days=2, watts=100.0), source="simulator")
        provenance = client.get("/api/analytics/summary").json()["data_provenance"]
        assert provenance["fully_simulated"] is True
        assert provenance["readings_from_hardware"] == 0
        assert provenance["device_is_simulated"] is True
        assert "No physical meter" in provenance["statement"]

    def test_summary_does_not_claim_simulation_when_data_is_real(self, client):
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", days=2, watts=100.0), source="esp32")
        provenance = client.get("/api/analytics/summary").json()["data_provenance"]
        assert provenance["fully_simulated"] is False
        assert provenance["readings_from_hardware"] > 0

    def test_empty_database_reports_no_device_rather_than_crashing(self, client):
        response = client.get("/api/analytics/summary")
        assert response.status_code == 200
        assert response.json()["status"] == "no_device"

    @pytest.mark.parametrize("path", ["/api/analytics/summary", "/api/ai/forecast", "/api/ai/anomalies", "/api/ai/insights"])
    def test_empty_state_answers_200_with_a_status_and_no_figures(self, client, path):
        """A brand new install has to render, not throw.

        Each of these panels used to read a nested field without checking that the endpoint had
        actually produced figures, so the very first page load on an empty database crashed the
        dashboard. A status-only body is the contract for "nothing to show yet".
        """
        body = client.get(path).json()
        assert body["status"] == "no_device"
        for absent in ("current_power", "projected_bill", "projected_bill_this_period"):
            assert absent not in body, f"{path} should not imply a bill exists"

    def test_empty_usage_response_keeps_the_populated_shape(self, client):
        """The usage chart reads range and bucket directly, so the empty answer must still be
        shaped like a real one, with nulls where there is genuinely nothing to report."""
        body = client.get("/api/analytics/usage").json()
        assert body["status"] == "no_device"
        assert body["range"] is None
        assert body["bucket"] is None
        assert body["buckets"] == []

    def test_summary_labels_model_output_as_an_estimate(self, client):
        post_readings(client, build_readings("d1", "2026-09-20T00:00:00Z", days=6, watts=300.0), source="simulator")
        summary = client.get("/api/analytics/summary").json()
        assert summary["projected_bill"]["is_estimate"] is True
        assert summary["energy_today"]["is_estimate"] is False
        assert summary["current_power"]["is_estimate"] is False


class TestSpaFallback:
    """The dashboard is served from the same origin as the API, so the catch-all route must not
    answer API paths. Handing back index.html with a 200 hides a client-side typo."""

    def test_an_unknown_api_path_returns_404_json_not_html(self, client):
        response = client.get("/api/there-is-no-such-endpoint")
        assert response.status_code == 404, "a mistyped API path must not be served the dashboard"
        assert "json" in response.headers.get("content-type", "")

    def test_a_known_api_path_still_returns_json(self, client):
        response = client.get("/api/ingestion/sources")
        assert response.status_code == 200
        assert response.json()["sources"], "the payload contract must list at least one source"
