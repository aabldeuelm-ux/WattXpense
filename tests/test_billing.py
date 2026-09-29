"""Billing arithmetic has to be exact, so these tests use known numbers and check the maths line by line."""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError


def set_flat_tariff(client, rate=0.30, standing=0.20, tax=10.0, name="Test flat"):
    response = client.post(
        "/api/billing/tariff",
        json={
            "name": name,
            "rate_type": "flat",
            "currency": "USD",
            "flat_rate_per_kwh": rate,
            "standing_charge_per_day": standing,
            "tax_percent": tax,
            "billing_start_day": 1,
            "is_active": True,
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["tariff"]


class TestFlatBilling:
    def test_energy_charge_is_rate_times_kwh(self, client):
        from tests.conftest import build_readings, post_readings

        set_flat_tariff(client, rate=0.30, standing=0.0, tax=0.0)
        # 10 W for 2 hours = 20 Wh = 0.02 kWh
        post_readings(client, build_readings("d1", "2026-09-25T00:00:00Z", days=1, watts=10.0))

        bill = client.get("/api/billing/preview?energy_kwh=10&days=5").json()["estimate"]
        assert bill["energy_charge"] == pytest.approx(3.0)  # 10 kWh x 0.30
        assert bill["standing_charge"] == pytest.approx(0.0)
        assert bill["tax"] == pytest.approx(0.0)
        assert bill["total"] == pytest.approx(3.0)

    def test_standing_charge_is_per_day(self, client):
        set_flat_tariff(client, rate=0.0, standing=0.25, tax=0.0)
        bill = client.get("/api/billing/preview?energy_kwh=5&days=4").json()["estimate"]
        assert bill["standing_charge"] == pytest.approx(1.0)  # 4 x 0.25

    def test_tax_applies_to_subtotal_not_to_energy_alone(self, client):
        set_flat_tariff(client, rate=1.00, standing=1.00, tax=10.0)
        bill = client.get("/api/billing/preview?energy_kwh=10&days=2").json()["estimate"]
        # energy 10.00 + standing 2.00 = subtotal 12.00, tax = 1.20
        assert bill["energy_charge"] == pytest.approx(10.0)
        assert bill["standing_charge"] == pytest.approx(2.0)
        assert bill["subtotal"] == pytest.approx(12.0)
        assert bill["tax"] == pytest.approx(1.2)
        assert bill["total"] == pytest.approx(13.2)

    def test_line_formulas_add_up_to_the_total(self, client):
        set_flat_tariff(client, rate=0.28, standing=0.15, tax=5.0)
        bill = client.get("/api/billing/preview?energy_kwh=123.456&days=17").json()["estimate"]
        summed = sum(line["amount"] for line in bill["lines"])
        assert summed == pytest.approx(bill["subtotal"] + bill["tax"], abs=0.02)
        assert bill["total"] == pytest.approx(summed, abs=0.02)
        for line in bill["lines"]:
            assert "=" in line["formula"], "every line must show its working"


class TestTimeOfUseBilling:
    def test_peak_and_offpeak_are_charged_differently(self, client):
        response = client.post(
            "/api/billing/tariff",
            json={
                "name": "Peak saver",
                "rate_type": "time_of_use",
                "currency": "USD",
                "peak_rate_per_kwh": 0.40,
                "offpeak_rate_per_kwh": 0.10,
                "peak_start_hour": 17,
                "peak_end_hour": 22,
                "standing_charge_per_day": 0.0,
                "tax_percent": 0.0,
                "billing_start_day": 1,
                "is_active": True,
            },
        )
        assert response.status_code == 200, response.text
        bill = client.get("/api/billing/preview?energy_kwh=100&days=10&peak_share=0.3").json()["estimate"]
        # 30 kWh at peak, 70 kWh off-peak
        assert bill["energy"]["peak_kwh"] == pytest.approx(30.0, abs=0.01)
        assert bill["energy"]["offpeak_kwh"] == pytest.approx(70.0, abs=0.01)
        assert bill["energy_charge"] == pytest.approx(30.0 * 0.40 + 70.0 * 0.10, abs=0.01)

    def test_peak_rate_below_offpeak_is_rejected(self, client):
        response = client.post(
            "/api/billing/tariff",
            json={
                "name": "Backwards",
                "rate_type": "time_of_use",
                "currency": "USD",
                "peak_rate_per_kwh": 0.10,
                "offpeak_rate_per_kwh": 0.40,
                "peak_start_hour": 17,
                "peak_end_hour": 22,
                "standing_charge_per_day": 0.0,
                "tax_percent": 0.0,
                "billing_start_day": 1,
                "is_active": True,
            },
        )
        assert response.status_code == 422

    def test_flat_tariff_without_a_rate_is_rejected(self, client):
        response = client.post(
            "/api/billing/tariff",
            json={
                "name": "No rate",
                "rate_type": "flat",
                "currency": "USD",
                "standing_charge_per_day": 0.0,
                "tax_percent": 0.0,
                "billing_start_day": 1,
                "is_active": True,
            },
        )
        assert response.status_code == 422


class TestBillingPeriod:
    def test_period_length_follows_the_start_day(self, client):
        from tests.conftest import build_readings, post_readings

        set_flat_tariff(client)
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", days=2, watts=100.0))
        period = client.get("/api/analytics/summary").json()["projected_bill"]["period"]
        assert period["start"] == "2026-09-01"
        assert period["end_inclusive"] == "2026-09-30"
        assert period["days_total"] == 30

    def test_thirty_one_day_month_is_handled(self, client, monkeypatch):
        from tests.conftest import build_readings, post_readings, set_now

        set_now(monkeypatch, "2026-07-17T12:00:00Z")
        set_flat_tariff(client)
        post_readings(client, build_readings("d1", "2026-07-01T00:00:00Z", days=5, watts=100.0))
        period = client.get("/api/analytics/summary").json()["projected_bill"]["period"]
        assert period["start"] == "2026-07-01"
        assert period["end_inclusive"] == "2026-07-31"
        assert period["days_total"] == 31

    def test_period_rolls_over_at_the_start_day(self, client, monkeypatch):
        from tests.conftest import build_readings, post_readings, set_now

        set_flat_tariff(client)
        post_readings(client, build_readings("d1", "2026-09-28T00:00:00Z", days=3, watts=100.0))
        set_now(monkeypatch, "2026-09-30T23:00:00Z")
        assert client.get("/api/analytics/summary").json()["projected_bill"]["period"]["start"] == "2026-09-01"
        set_now(monkeypatch, "2026-10-01T00:30:00Z")
        period = client.get("/api/analytics/summary").json()["projected_bill"]["period"]
        assert period["start"] == "2026-10-01"
        assert period["days_elapsed"] == 1


class TestBillHonesty:
    def test_projection_is_flagged_as_an_estimate(self, client):
        from tests.conftest import build_readings, post_readings

        set_flat_tariff(client)
        post_readings(client, build_readings("d1", "2026-09-24T00:00:00Z", days=2, watts=100.0))
        bill = client.get("/api/analytics/summary").json()["projected_bill"]
        assert bill["is_estimate"] is True
        assert bill["is_projection"] is True
        assert bill["projection_basis"]
        assert any("estimate" in line.lower() for line in bill["explanation"])

    def test_bill_metered_energy_is_not_double_counted(self, client):
        """Total billed energy must never exceed what the meter recorded, even with a forecast on top."""
        from tests.conftest import build_readings, post_readings

        set_flat_tariff(client)
        # 4 full days at 1 kW = 96 kWh metered inside September.
        post_readings(client, build_readings("d1", "2026-09-20T00:00:00Z", days=4, watts=1000.0))
        summary = client.get("/api/analytics/summary").json()
        metered = summary["projected_bill"]["energy"]["total_kwh"]
        assert metered == pytest.approx(96.0, abs=0.5)
        assert summary["projected_bill"]["forecast_covers_today"] in (True, False)


def set_slab_tariff(client, slabs=None, standing=0.0, tax=0.0, name="Test slab", currency="INR"):
    """A three-block slab tariff by default: 0-100 at 2.50, 100-300 at 4.50, 300+ at 6.50."""
    response = client.post(
        "/api/billing/tariff",
        json={
            "name": name,
            "rate_type": "slab",
            "currency": currency,
            "standing_charge_per_day": standing,
            "tax_percent": tax,
            "billing_start_day": 1,
            "is_active": True,
            "slabs": slabs
            or [
                {"up_to_kwh": 100, "rate_per_kwh": 2.5},
                {"up_to_kwh": 300, "rate_per_kwh": 4.5},
                {"up_to_kwh": None, "rate_per_kwh": 6.5},
            ],
        },
    )
    assert response.status_code == 200, response.text
    return response.json()["tariff"]


class TestSlabArithmetic:
    """A slab tariff charges each unit at the rate of the block it falls in, never a single blended rate."""

    def test_each_unit_is_charged_at_its_own_slab_rate(self, client):
        set_slab_tariff(client)
        bill = client.get("/api/billing/preview?energy_kwh=450&days=30").json()["estimate"]
        # 100 x 2.50 = 250, then 200 x 4.50 = 900, then 150 x 6.50 = 975.
        assert bill["energy_charge"] == pytest.approx(2125.0)

    def test_a_blended_rate_would_be_wrong_and_is_not_used(self, client):
        """450 units at the top rate would be 2925, and at the bottom rate 1125. Neither is the bill."""
        set_slab_tariff(client)
        bill = client.get("/api/billing/preview?energy_kwh=450&days=30").json()["estimate"]
        assert bill["energy_charge"] != pytest.approx(450 * 6.5)
        assert bill["energy_charge"] != pytest.approx(450 * 2.5)

    def test_consumption_below_the_first_boundary_uses_only_that_slab(self, client):
        set_slab_tariff(client)
        bill = client.get("/api/billing/preview?energy_kwh=60&days=30").json()["estimate"]
        assert bill["energy_charge"] == pytest.approx(150.0)
        energy_lines = [line for line in bill["lines"] if line["kind"] == "metered"]
        assert len(energy_lines) == 1

    def test_every_slab_gets_its_own_line_with_its_own_range(self, client):
        set_slab_tariff(client)
        bill = client.get("/api/billing/preview?energy_kwh=450&days=30").json()["estimate"]
        energy_lines = [line for line in bill["lines"] if line["kind"] == "metered"]
        assert len(energy_lines) == 3
        assert "First 100" in energy_lines[0]["label"]
        assert "101-300" in energy_lines[1]["label"]
        assert "Above 300" in energy_lines[2]["label"]
        quantities = [line["quantity"] for line in energy_lines]
        assert quantities == [100.0, 200.0, 150.0]
        assert sum(quantities) == pytest.approx(450.0)

    def test_zero_consumption_costs_nothing(self, client):
        set_slab_tariff(client, standing=2.5, tax=5.0)
        bill = client.get("/api/billing/preview?energy_kwh=0&days=30").json()["estimate"]
        assert bill["energy_charge"] == pytest.approx(0.0)
        assert bill["total"] == pytest.approx(75.0)  # the fixed charge still applies

    def test_consumption_above_the_highest_boundary_is_still_billed(self, client):
        set_slab_tariff(client)
        bill = client.get("/api/billing/preview?energy_kwh=800&days=30").json()["estimate"]
        assert bill["energy_charge"] == pytest.approx(250 + 900 + 3250)  # 500 beyond 300 at 6.50
        assert any("open-ended" in note or "Above" in line["label"] for line in bill["lines"] for note in [line["label"]])

    def test_duty_applies_to_the_energy_charge_only(self, client):
        set_slab_tariff(client, standing=2.5, tax=5.0)
        bill = client.get("/api/billing/preview?energy_kwh=250&days=30").json()["estimate"]
        assert bill["energy_charge"] == pytest.approx(925.0)  # 250 + 675
        assert bill["standing_charge"] == pytest.approx(75.0)
        assert bill["tax"] == pytest.approx(46.25)  # 5% of 925, not of 1000
        assert bill["total"] == pytest.approx(1046.25)

    def test_the_duty_is_named_the_way_an_indian_bill_names_it(self, client):
        set_slab_tariff(client, tax=5.0)
        bill = client.get("/api/billing/preview?energy_kwh=100&days=30").json()["estimate"]
        assert bill["duty_label"] == "Electricity duty"
        assert any("Electricity duty" in line["label"] for line in bill["lines"])

    def test_a_non_indian_currency_keeps_the_generic_levy_name(self, client):
        set_slab_tariff(client, currency="USD", tax=5.0)
        bill = client.get("/api/billing/preview?energy_kwh=100&days=30").json()["estimate"]
        assert bill["duty_label"] == "Tax / levy"

    def test_progressive_totals_increase_by_the_marginal_rate_at_each_boundary(self, client):
        set_slab_tariff(client)
        totals = []
        for kwh in (100, 300, 500):
            bill = client.get(f"/api/billing/preview?energy_kwh={kwh}&days=30").json()["estimate"]
            totals.append(bill["energy_charge"])
        assert totals[1] - totals[0] == pytest.approx(200 * 4.5)
        assert totals[2] - totals[1] == pytest.approx(200 * 6.5)


class TestSlabValidation:
    def _post(self, client, **overrides):
        payload = {
            "name": "Bad slab",
            "rate_type": "slab",
            "currency": "INR",
            "slabs": [
                {"up_to_kwh": 100, "rate_per_kwh": 2.5},
                {"up_to_kwh": None, "rate_per_kwh": 4.5},
            ],
        }
        payload.update(overrides)
        return client.post("/api/billing/tariff", json=payload)

    def test_a_slab_tariff_with_no_slabs_is_rejected(self, client):
        assert self._post(client, slabs=[]).status_code == 422

    def test_a_tariff_whose_top_slab_is_not_open_ended_is_rejected(self, client):
        response = self._post(
            client, slabs=[{"up_to_kwh": 100, "rate_per_kwh": 2.5}, {"up_to_kwh": 300, "rate_per_kwh": 4.5}]
        )
        assert response.status_code == 422
        assert "open ended" in str(response.json())

    def test_an_open_ended_slab_that_is_not_last_is_rejected(self, client):
        response = self._post(
            client,
            slabs=[
                {"up_to_kwh": None, "rate_per_kwh": 2.5},
                {"up_to_kwh": 300, "rate_per_kwh": 4.5},
                {"up_to_kwh": None, "rate_per_kwh": 6.5},
            ],
        )
        assert response.status_code == 422

    def test_slabs_whose_bounds_do_not_increase_are_rejected(self, client):
        response = self._post(
            client,
            slabs=[
                {"up_to_kwh": 300, "rate_per_kwh": 2.5},
                {"up_to_kwh": 100, "rate_per_kwh": 4.5},
                {"up_to_kwh": None, "rate_per_kwh": 6.5},
            ],
        )
        assert response.status_code == 422

    def test_slabs_whose_rates_fall_are_rejected(self, client):
        response = self._post(client, slabs=[{"up_to_kwh": 100, "rate_per_kwh": 8}, {"up_to_kwh": None, "rate_per_kwh": 4}])
        assert response.status_code == 422
        assert "must not decrease" in str(response.json())

    def test_a_negative_slab_rate_is_rejected(self, client):
        response = self._post(client, slabs=[{"up_to_kwh": None, "rate_per_kwh": -1}])
        assert response.status_code == 422

    def test_equal_rates_across_slabs_are_allowed(self, client):
        response = self._post(client, slabs=[{"up_to_kwh": 100, "rate_per_kwh": 5}, {"up_to_kwh": None, "rate_per_kwh": 5}])
        assert response.status_code == 200

    def test_a_single_open_ended_slab_is_allowed(self, client):
        response = self._post(client, slabs=[{"up_to_kwh": None, "rate_per_kwh": 6}])
        assert response.status_code == 200


class TestSlabRatesInAnalytics:
    def test_the_period_summary_prices_energy_progressively(self, client):
        from tests.conftest import build_readings, post_readings

        set_slab_tariff(client)
        # 25 days at 400 W = 240 kWh, which spans the first two blocks.
        post_readings(client, build_readings("d1", "2026-09-01T00:00:00Z", days=25, watts=400.0))
        period = client.get("/api/analytics/summary").json()["energy_this_period"]
        assert period["energy_kwh"] == pytest.approx(240.0, abs=1.0)
        assert period["cost"] == pytest.approx(250 + 140 * 4.5)  # 880
        assert period["cost_basis"] == "progressive_slabs"

    def test_todays_cost_uses_the_marginal_rate_not_the_average(self, client):
        from tests.conftest import build_readings, post_readings

        set_slab_tariff(client)
        post_readings(client, build_readings("d1", "2026-09-01T00:00:00Z", days=25, watts=400.0))
        today = client.get("/api/analytics/summary").json()["energy_today"]
        # 238 units were already metered today, so today's units sit in the second block at 4.50,
        # not at the 3.67 blended average and not at the 2.50 bottom rate.
        assert today["cost_basis"] == "marginal_slab_rate"
        assert today["cost"] == pytest.approx(today["energy_kwh"] * 4.5)

    def test_a_slab_today_bill_excludes_the_period_fixed_charge_and_duty(self, client):
        from tests.conftest import build_readings, post_readings

        set_slab_tariff(client, standing=2.5, tax=5.0)
        post_readings(client, build_readings("d1", "2026-09-01T00:00:00Z", days=25, watts=400.0))
        detail = client.get("/api/analytics/summary").json()["today_bill_detail"]
        assert detail["standing_charge"] == pytest.approx(0.0)
        assert detail["tax"] == pytest.approx(0.0)
        assert any("slab" in note for note in detail["notes"])

    def test_insights_do_not_quote_a_blended_slab_rate_as_a_fixed_price(self, client):
        from tests.conftest import build_readings, post_readings

        set_slab_tariff(client)
        post_readings(client, build_readings("d1", "2026-09-01T00:00:00Z", days=25, watts=400.0))
        report = client.get("/api/ai/insights").json()
        text = " ".join(
            assumption for insight in report.get("insights", []) for assumption in insight.get("assumptions", [])
        )
        assert "average across the slabs" in text

    def test_effective_rate_is_reported_as_blinded_not_flat_when_consumption_is_known(self, client):
        from backend.app.database import SessionLocal
        from backend.app.models import Tariff
        from backend.app.services.tariffs import effective_rate, rate_basis, to_config

        set_slab_tariff(client)
        with SessionLocal() as db:
            config = to_config(db.query(Tariff).filter(Tariff.is_active.is_(True)).one())
        assert effective_rate(config) == pytest.approx(6.5)
        assert rate_basis(config) == "highest_slab"
        assert effective_rate(config, 240.0) == pytest.approx(880 / 240, abs=0.01)
        assert rate_basis(config, 240.0) == "blended_across_slabs"


class TestSchemaMigration:
    """An existing database must gain new columns without losing the rows already in it."""

    def test_an_existing_database_gains_the_slabs_column_and_keeps_its_tariff(self, tmp_path):
        import sqlite3

        from backend.app.database import Base, _add_missing_columns, engine

        old_db = tmp_path / "old.db"
        connection = sqlite3.connect(old_db)
        # Exactly the tariffs table as it looked before slab support existed.
        connection.executescript(
            """
            CREATE TABLE tariffs (
                id INTEGER NOT NULL PRIMARY KEY AUTOINCREMENT,
                name VARCHAR(120) NOT NULL,
                rate_type VARCHAR(16) NOT NULL,
                currency VARCHAR(8) NOT NULL,
                flat_rate_per_kwh FLOAT,
                peak_rate_per_kwh FLOAT,
                offpeak_rate_per_kwh FLOAT,
                peak_start_hour INTEGER,
                peak_end_hour INTEGER,
                standing_charge_per_day FLOAT NOT NULL,
                tax_percent FLOAT NOT NULL,
                billing_start_day INTEGER NOT NULL,
                is_active BOOLEAN NOT NULL,
                created_at DATETIME NOT NULL
            );
            INSERT INTO tariffs (name, rate_type, currency, flat_rate_per_kwh, standing_charge_per_day,
                                 tax_percent, billing_start_day, is_active, created_at)
            VALUES ('Old household tariff', 'flat', 'USD', 0.28, 0.15, 5, 1, 1, '2026-01-01 00:00:00');
            """
        )
        connection.commit()
        connection.close()

        columns_before = {row[1] for row in sqlite3.connect(old_db).execute("PRAGMA table_info(tariffs)")}
        assert "slabs" not in columns_before

        # Point a fresh metadata create at that file, then run the additive migration.
        from sqlalchemy import create_engine

        legacy_engine = create_engine(f"sqlite:///{old_db}")
        Base.metadata.create_all(bind=legacy_engine)
        import backend.app.database as database_module

        original = database_module.engine
        try:
            database_module.engine = legacy_engine
            database_module._add_missing_columns()
        finally:
            database_module.engine = original
            legacy_engine.dispose()

        columns_after = {row[1] for row in sqlite3.connect(old_db).execute("PRAGMA table_info(tariffs)")}
        assert "slabs" in columns_after
        assert columns_before.issubset(columns_after)  # nothing was dropped

        saved = sqlite3.connect(old_db).execute("SELECT name, currency, flat_rate_per_kwh FROM tariffs").fetchall()
        assert saved == [("Old household tariff", "USD", 0.28)]

    def test_migrating_twice_changes_nothing(self, tmp_path):
        import sqlite3

        from sqlalchemy import create_engine

        from backend.app.database import Base, _add_missing_columns
        import backend.app.database as database_module

        target = tmp_path / "twice.db"
        engine_for_file = create_engine(f"sqlite:///{target}")
        Base.metadata.create_all(bind=engine_for_file)
        original = database_module.engine
        try:
            database_module.engine = engine_for_file
            _add_missing_columns()
            _add_missing_columns()  # must be safe to run on every start
        finally:
            database_module.engine = original
            engine_for_file.dispose()
        assert "slabs" in {row[1] for row in sqlite3.connect(target).execute("PRAGMA table_info(tariffs)")}


class TestIndiaDefaults:
    """A fresh install should come up already set up for an Indian household."""

    def test_the_default_tariff_is_an_inr_slab_tariff(self, client):
        tariff = client.get("/api/billing/tariff").json()["tariff"]
        assert tariff["currency"] == "INR"
        assert tariff["rate_type"] == "slab"
        assert len(tariff["slabs"]) >= 2
        assert tariff["slabs"][-1]["up_to_kwh"] is None  # open ended, so nothing goes unpriced
        assert "example" in tariff["name"].lower()  # not passed off as a real DISCOM schedule

    def test_the_default_tariff_is_labelled_as_an_electricity_duty(self, client):
        tariff = client.get("/api/billing/tariff").json()["tariff"]
        assert tariff["duty_label"] == "Electricity duty"

    def test_the_default_timezone_is_ist(self, monkeypatch):
        """The suite pins the timezone to UTC for determinism, so the default has to be read directly."""
        from backend.app.config import get_settings

        monkeypatch.delenv("WATTXPENSE_TIMEZONE", raising=False)
        get_settings.cache_clear()
        try:
            assert get_settings().timezone_name == "Asia/Kolkata"
        finally:
            # Put the suite's pinned clock back so later tests stay deterministic.
            monkeypatch.setenv("WATTXPENSE_TIMEZONE", "UTC")
            get_settings.cache_clear()

    def test_slabs_get_stricter_as_consumption_rises(self, client):
        tariff = client.get("/api/billing/tariff").json()["tariff"]
        rates = [slab["rate_per_kwh"] for slab in tariff["slabs"]]
        assert rates == sorted(rates)

    def test_the_peak_window_is_the_indian_evening(self, client):
        from backend.app.config import get_settings

        settings = get_settings()
        assert settings.default_peak_start_hour == 18
        assert settings.default_peak_end_hour == 22

    def test_supply_voltage_and_frequency_are_the_indian_ones(self, client):
        from backend.app.config import get_settings

        settings = get_settings()
        assert settings.nominal_voltage_v == 230
        assert settings.nominal_frequency_hz == 50

    def test_a_malformed_slab_override_falls_back_instead_of_zero_rating(self, monkeypatch):
        from backend.app.config import DEFAULT_INDIA_DOMESTIC_SLABS, _env_slabs

        monkeypatch.setenv("WATTXPENSE_DEFAULT_SLABS", "{not json")
        assert _env_slabs("WATTXPENSE_DEFAULT_SLABS", DEFAULT_INDIA_DOMESTIC_SLABS) == DEFAULT_INDIA_DOMESTIC_SLABS

        monkeypatch.setenv(
            "WATTXPENSE_DEFAULT_SLABS", '[{"up_to_kwh": 100, "rate_per_kwh": 3.0}, {"up_to_kwh": 200, "rate_per_kwh": 5.0}]'
        )
        # No open-ended top slab, so usage above 200 units would be unpriced.
        assert _env_slabs("WATTXPENSE_DEFAULT_SLABS", DEFAULT_INDIA_DOMESTIC_SLABS) == DEFAULT_INDIA_DOMESTIC_SLABS

        monkeypatch.setenv("WATTXPENSE_DEFAULT_SLABS", '[{"up_to_kwh": null, "rate_per_kwh": 3.0}]')
        assert _env_slabs("WATTXPENSE_DEFAULT_SLABS", DEFAULT_INDIA_DOMESTIC_SLABS) == ({"up_to_kwh": None, "rate_per_kwh": 3.0},)

    @pytest.mark.parametrize(
        "override",
        [
            # An open-ended block in the middle would swallow every band above it and leave the
            # last one unpriced, which is the failure mode this parser has to refuse.
            '[{"up_to_kwh": null, "rate_per_kwh": 2.0}, {"up_to_kwh": 300, "rate_per_kwh": 5.0}]',
            # Bounds that do not strictly increase mean the second block prices zero units.
            '[{"up_to_kwh": 300, "rate_per_kwh": 2.0}, {"up_to_kwh": 300, "rate_per_kwh": 5.0},'
            ' {"up_to_kwh": null, "rate_per_kwh": 8.0}]',
            '[{"up_to_kwh": 300, "rate_per_kwh": 2.0}, {"up_to_kwh": 100, "rate_per_kwh": 5.0},'
            ' {"up_to_kwh": null, "rate_per_kwh": 8.0}]',
            # A cheaper rate in a higher band breaks the progressive promise of the preset.
            '[{"up_to_kwh": 100, "rate_per_kwh": 5.0}, {"up_to_kwh": 300, "rate_per_kwh": 2.0},'
            ' {"up_to_kwh": null, "rate_per_kwh": 8.0}]',
        ],
    )
    def test_a_slab_override_the_api_would_reject_falls_back(self, monkeypatch, override):
        """The environment override is held to the same rules as the REST payload.

        Otherwise a bad value in the environment would install a default tariff that the API
        later refuses, and the operator would be left with a broken billing config they never
        typed into the UI.
        """
        from backend.app.config import DEFAULT_INDIA_DOMESTIC_SLABS, _env_slabs
        from backend.app.schemas import TariffIn

        monkeypatch.setenv("WATTXPENSE_DEFAULT_SLABS", override)
        assert _env_slabs("WATTXPENSE_DEFAULT_SLABS", DEFAULT_INDIA_DOMESTIC_SLABS) == DEFAULT_INDIA_DOMESTIC_SLABS

        # The same structure sent through the API is rejected, which is the point.
        with pytest.raises(ValidationError):
            TariffIn(rate_type="slab", currency="INR", slabs=json.loads(override))
