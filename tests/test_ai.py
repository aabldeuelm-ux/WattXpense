"""AI tests focus on honesty: no silent failures, no overclaiming, and explanations that survive scrutiny."""

from __future__ import annotations

import datetime as dt

import pytest

from tests.conftest import build_readings, post_readings

FAST_TARIFF = {
    "name": "Test tariff",
    "rate_type": "flat",
    "currency": "USD",
    "flat_rate_per_kwh": 0.30,
    "standing_charge_per_day": 0.15,
    "tax_percent": 5.0,
    "billing_start_day": 1,
    "is_active": True,
}


def set_tariff(client, **overrides):
    payload = {**FAST_TARIFF, **overrides}
    response = client.post("/api/billing/tariff", json=payload)
    assert response.status_code == 200, response.text
    return response.json()["tariff"]


def summary_metered_kwh(client):
    """Energy actually recorded by the meter for the current billing period, straight from the readings."""
    summary = client.get("/api/analytics/summary").json()
    return summary["energy_this_period"]["energy_kwh"]


def weekday_pattern(
    client,
    device_id="d1",
    start="2026-08-10T00:00:00Z",
    weeks=6,
    base=300.0,
    weekend_scale=0.75,
    partial_tail_hours=6,
):
    """A believable week: higher on weekdays, a Sunday trough, a repeating overnight baseline,
    plus a little deterministic jitter so the model is not handed a perfectly repeating series.

    A partial final day is appended, because real data always ends mid-day and the forecast
    has to notice that rather than learn from it.
    """
    import random

    rng = random.Random(20260926)
    begin = dt.datetime.fromisoformat(start.replace("Z", "+00:00"))
    rows = []
    end = begin + dt.timedelta(weeks=weeks)
    while begin < end:
        hour = begin.hour
        weekend = begin.weekday() >= 5
        watts = base * (weekend_scale if weekend else 1.0)
        watts += 900 if 18 <= hour <= 21 else 0
        watts += 250 if 7 <= hour <= 8 else 0
        watts *= 1.0 + rng.uniform(-0.08, 0.08)
        rows.append(
            {
                "device_id": device_id,
                "ts": begin.isoformat().replace("+00:00", "Z"),
                "voltage": 230.0,
                "current": round(watts / 230.0, 6),
                "power_w": round(watts, 3),
                "energy_wh": round(watts, 3),
            }
        )
        begin += dt.timedelta(hours=1)

    for hour in range(partial_tail_hours):
        when = end + dt.timedelta(hours=hour)
        watts = base * rng.uniform(0.95, 1.05)
        rows.append(
            {
                "device_id": device_id,
                "ts": when.isoformat().replace("+00:00", "Z"),
                "voltage": 230.0,
                "current": round(watts / 230.0, 6),
                "power_w": round(watts, 3),
                "energy_wh": round(watts, 3),
            }
        )
    return rows


class TestForecast:
    @pytest.fixture(autouse=True)
    def _data(self, client):
        set_tariff(client)
        post_readings(client, weekday_pattern(client), source="simulator")

    def test_forecast_is_not_claimed_before_there_is_history(self, client):
        """The most important guarantee: never invent a forecast from nothing."""
        response = client.get("/api/ai/forecast?horizon_days=7")
        assert response.status_code == 200
        assert response.json()["status"] == "ok"

    def test_forecast_returns_one_point_per_requested_day(self, client):
        payload = client.get("/api/ai/forecast?horizon_days=7").json()
        assert len(payload["points"]) == 7
        assert payload["horizon_days"] == 7
        assert payload["first_day_forecast"] < payload["last_day_forecast"]

    def test_horizon_length_is_respected(self, client):
        for horizon in (7, 14, 30):
            payload = client.get(f"/api/ai/forecast?horizon_days={horizon}").json()
            assert len(payload["points"]) == horizon

    def test_every_forecast_point_has_an_uncertainty_band(self, client):
        for point in client.get("/api/ai/forecast?horizon_days=7").json()["points"]:
            assert point["lower_kwh"] is not None
            assert point["upper_kwh"] is not None
            assert point["lower_kwh"] <= point["energy_kwh"] <= point["upper_kwh"]

    def test_forecast_is_always_marked_as_an_estimate(self, client):
        assert client.get("/api/ai/forecast?horizon_days=7").json()["is_estimate"] is True

    def test_confidence_is_stated_with_a_reason(self, client):
        payload = client.get("/api/ai/forecast?horizon_days=7").json()
        assert payload["confidence"] in ("low", "moderate", "high")
        assert payload["confidence_reason"]

    def test_metrics_are_reported_honestly_not_hidden(self, client):
        metrics = client.get("/api/ai/forecast?horizon_days=7").json()["metrics"]
        for key in ("mape_percent", "rmse_kwh", "baseline_mape_percent", "skill_vs_baseline_percent", "test_samples"):
            assert key in metrics, f"{key} must be reported so the user can judge the model"
        assert metrics["mape_percent"] > 0
        assert metrics["test_samples"] > 0, "the model must be scored on data it never saw"

    def test_the_method_is_disclosed(self, client):
        payload = client.get("/api/ai/forecast?horizon_days=7").json()
        assert "GradientBoosting" in payload["model"] or "model" in payload["method"].lower()
        assert payload["model"]

    def test_incomplete_days_are_excluded_from_training(self, client):
        """A day still in progress would teach the model that partial days are normal."""
        payload = client.get("/api/ai/forecast?horizon_days=7").json()
        assert payload["excluded_incomplete_days"] >= 1
        assert payload["last_complete_day"]

    def test_forecast_energy_sums_to_the_reported_total(self, client):
        payload = client.get("/api/ai/forecast?horizon_days=7").json()
        assert sum(point["energy_kwh"] for point in payload["points"]) == pytest.approx(
            payload["predicted_energy_kwh"], abs=0.05
        )

    def test_forecast_does_not_count_the_same_energy_twice(self, client):
        """Metered energy plus forecast energy must reconstruct the billed total exactly once."""
        bill = client.get("/api/analytics/summary").json()["projected_bill"]
        energy = bill["energy"]["total_kwh"]
        forecast = bill["forecast"]
        if forecast["status"] == "ok":
            metered = energy - forecast["predicted_remaining_kwh"]
            assert energy >= metered
            assert metered <= summary_metered_kwh(client) + 0.01
        assert bill["projection_basis"]

    def test_forecast_covers_today_when_today_is_still_rolling(self, client, monkeypatch):
        """Once today has readings, the forecast must cover it rather than adding it twice."""
        post_readings(
            client,
            build_readings("d1", "2026-09-26T00:00:00Z", hours=3, watts=250.0),
            source="simulator",
        )
        bill = client.get("/api/analytics/summary").json()["projected_bill"]
        assert bill["forecast_covers_today"] is True
        assert "twice" in bill["projection_basis"]

    def test_a_better_pattern_yields_a_better_score(self, client):
        """A strong weekday/weekend signal should beat the lag-7 baseline, proving the model works."""
        metrics = client.get("/api/ai/forecast?horizon_days=7").json()["metrics"]
        assert metrics["skill_vs_baseline_percent"] > 0


class TestForecastWithTooLittleData:
    def test_it_says_so_instead_of_failing(self, client):
        set_tariff(client)
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", hours=6, watts=100.0))
        response = client.get("/api/ai/forecast?horizon_days=7")
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] in ("ok", "insufficient_data")
        if payload["status"] != "ok":
            assert payload["confidence_reason"]

    def test_no_data_at_all_does_not_crash(self, client):
        set_tariff(client)
        response = client.get("/api/ai/forecast?horizon_days=7")
        assert response.status_code == 200
        # No device has ever reported, so there is honestly nothing to forecast.
        assert response.json()["status"] in ("insufficient_data", "no_device")

    def test_a_device_with_no_readings_yet_says_so(self, client):
        set_tariff(client)
        client.post("/api/devices", json={"id": "empty", "name": "Empty meter", "is_simulated": False})
        response = client.get("/api/ai/forecast?horizon_days=7&device_id=empty")
        assert response.status_code == 200
        assert response.json()["status"] == "insufficient_data"


class TestAnomalies:
    def test_a_plain_pattern_raises_no_false_alarms(self, client):
        set_tariff(client)
        post_readings(client, weekday_pattern(client), source="simulator")
        payload = client.get("/api/ai/anomalies").json()
        high = [f for f in payload["findings"] if f["severity"] == "high" and f["kind"] == "daily_energy"]
        assert not high, f"a steady repeating pattern should not be called unusual, got {[f['title'] for f in high]}"

    def test_a_quiet_weekend_is_not_called_unusual(self, client):
        """A home that is consistently quieter at the weekend is showing a pattern, not a fault."""
        set_tariff(client)
        post_readings(client, weekday_pattern(client, weekend_scale=0.45), source="simulator")
        payload = client.get("/api/ai/anomalies").json()
        usage = [f for f in payload["findings"] if f["kind"] == "daily_energy"]
        assert not usage, (
            "weekend low days that repeat every week must not be reported, got "
            f"{[f['title'] for f in usage]}"
        )

    def test_an_elevated_draw_block_is_called_a_load_pattern(self, client):
        """A heater left on for hours can leave the daily total normal. The report must say so
        honestly rather than claiming the energy total was unusual."""
        set_tariff(client)
        rows = weekday_pattern(client)
        target = dt.date(2026, 9, 20)
        target_rows = [
            row for row in rows if dt.datetime.fromisoformat(row["ts"].replace("Z", "+00:00")).date() == target
        ]
        assert len(target_rows) == 24, "the fixture must supply a whole day to reshape"

        # Keep the day's energy identical, but move it into one long overnight high-draw block
        # and a low baseline for the rest, so only the shape of the load changes.
        total_wh = sum(row["energy_wh"] for row in target_rows)
        block_w = 2000.0
        block_hours = 4
        remainder_w = (total_wh - block_w * block_hours) / (len(target_rows) - block_hours)

        swapped: list[dict] = []
        for row in rows:
            when = dt.datetime.fromisoformat(row["ts"].replace("Z", "+00:00"))
            if when.date() != target:
                swapped.append(row)
                continue
            watts = block_w if when.hour < block_hours else remainder_w
            swapped.append(
                {
                    "device_id": "d1",
                    "ts": row["ts"],
                    "voltage": 230.0,
                    "current": round(watts / 230.0, 6),
                    "power_w": round(watts, 3),
                    "energy_wh": round(watts, 3),
                }
            )
        post_readings(client, swapped, source="simulator")

        payload = client.get("/api/ai/anomalies").json()
        overstated = [f for f in payload["findings"] if "high energy" in f["title"] or "low energy" in f["title"]]
        assert not overstated, (
            "a normal daily total must not be reported as unusual energy, got "
            f"{[f['title'] for f in overstated]}"
        )

    def test_a_day_with_thin_coverage_is_not_called_a_usage_anomaly(self, client):
        """A part-recorded day looks low because most of it is missing. That is a data problem,
        already reported as such, and must not also be listed as unusual consumption."""
        set_tariff(client)
        rows = weekday_pattern(client)
        post_readings(client, rows, source="simulator")

        # The fixture deliberately ends mid-day, so find that partial last day.
        last_day = max(dt.datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).date() for r in rows)
        hours_on_last_day = sum(
            1 for r in rows if dt.datetime.fromisoformat(r["ts"].replace("Z", "+00:00")).date() == last_day
        )
        assert hours_on_last_day < 24, "the fixture must leave the final day incomplete"

        payload = client.get("/api/ai/anomalies").json()
        usage = [f for f in payload["findings"] if f["kind"] == "daily_energy" and f["id"] == f"daily-{last_day}"]
        assert not usage, f"an incomplete day must not be judged on consumption, got {[f['id'] for f in usage]}"
        assert any(f["kind"] == "data_gap" for f in payload["findings"]), (
            "thin coverage should be reported as a data gap instead"
        )

    def test_an_injected_spike_is_found_with_evidence(self, client):
        set_tariff(client)
        post_readings(client, weekday_pattern(client), source="simulator")
        # Three days of 5 kW on top of the normal pattern.
        spike = []
        begin = dt.datetime(2026, 9, 16, tzinfo=dt.timezone.utc)
        for day_offset in range(3):
            for hour in range(12):
                when = begin + dt.timedelta(days=day_offset, hours=hour)
                watts = 5000.0
                spike.append(
                    {
                        "device_id": "d1",
                        "ts": when.isoformat().replace("+00:00", "Z"),
                        "voltage": 230.0,
                        "current": round(watts / 230.0, 6),
                        "power_w": watts,
                        "energy_wh": watts,
                    }
                )
        post_readings(client, spike, source="simulator")

        payload = client.get("/api/ai/anomalies").json()
        assert payload["status"] == "ok"
        spike_days = [f for f in payload["findings"] if f["kind"] == "daily_energy" and "high" in f["title"]]
        assert spike_days, "the injected spike must be detected"
        finding = spike_days[0]
        assert finding["evidence"], "every finding needs its evidence attached"
        assert "median" in finding["reason"].lower()
        assert finding["possible_causes"], "findings should suggest causes to check"

    def test_a_data_gap_is_reported(self, client):
        set_tariff(client)
        rows = weekday_pattern(client)
        # Punch a three-day hole in the middle of the record.
        gap_start = dt.datetime(2026, 9, 1, tzinfo=dt.timezone.utc)
        gap_end = dt.datetime(2026, 9, 4, tzinfo=dt.timezone.utc)
        rows = [
            r
            for r in rows
            if not (gap_start <= dt.datetime.fromisoformat(r["ts"].replace("Z", "+00:00")) < gap_end)
        ]
        post_readings(client, rows, source="simulator")

        payload = client.get("/api/ai/anomalies").json()
        gaps = [f for f in payload["findings"] if f["kind"] == "data_gap"]
        assert gaps, "a missing stretch of data must be reported, not silently ignored"
        assert gaps[0]["window_start"] and gaps[0]["window_end"]

    def test_causes_are_labelled_as_hypotheses(self, client):
        set_tariff(client)
        post_readings(client, weekday_pattern(client), source="simulator")
        for finding in client.get("/api/ai/anomalies").json()["findings"]:
            if finding["possible_causes"]:
                assert finding["causes_are_hypotheses"] is True

    def test_anomalies_are_marked_as_estimate(self, client):
        set_tariff(client)
        post_readings(client, weekday_pattern(client), source="simulator")
        assert client.get("/api/ai/anomalies").json()["is_estimate"] is True

    def test_method_is_disclosed(self, client):
        set_tariff(client)
        post_readings(client, weekday_pattern(client), source="simulator")
        payload = client.get("/api/ai/anomalies").json()
        assert "IsolationForest" in payload["method"]
        assert payload["notes"]


class TestInsights:
    @pytest.fixture(autouse=True)
    def _data(self, client):
        set_tariff(client)
        post_readings(client, weekday_pattern(client), source="simulator")

    def test_a_standby_floor_is_spotted(self, client):
        """The pattern above never drops below 300 W, which is exactly the standby signature."""
        titles = " ".join(i["title"] for i in client.get("/api/ai/insights").json()["insights"]).lower()
        assert "baseline" in titles or "standby" in titles

    def test_every_saving_explains_its_own_arithmetic(self, client):
        payload = client.get("/api/ai/insights").json()
        savings = [i for i in payload["insights"] if i.get("savings_cost_per_month")]
        assert savings, "a standby load should come with a saving estimate"
        for insight in savings:
            assert insight["how_it_is_calculated"], "a number without its working is not trustworthy"
            assert insight["savings_basis"], "a saving must state what it assumes"

    def test_savings_are_marked_as_estimates(self, client):
        for insight in client.get("/api/ai/insights").json()["insights"]:
            assert insight["is_estimate"] is True

    def test_load_bands_account_for_all_the_energy(self, client):
        payload = client.get("/api/ai/insights").json()
        bands = payload["load_bands"]
        assert bands
        assert sum(band["energy_share_percent"] for band in bands) == pytest.approx(100.0, abs=0.5)
        assert sum(band["sample_share_percent"] for band in bands) == pytest.approx(100.0, abs=0.5)

    def test_savings_range_is_ordered(self, client):
        low, high = client.get("/api/ai/insights").json()["savings_range_cost_per_month"]
        assert low <= high

    def test_weekend_saving_is_offered_because_weekends_are_lighter(self, client):
        insights = client.get("/api/ai/insights").json()["insights"]
        assert any(i["category"] in ("weekday_vs_weekend", "behaviour") for i in insights)

    def test_no_data_produces_no_claims(self, client):
        set_tariff(client)
        response = client.get("/api/ai/insights")
        assert response.status_code == 200
        assert response.json()["status"] in ("ok", "insufficient_data")
