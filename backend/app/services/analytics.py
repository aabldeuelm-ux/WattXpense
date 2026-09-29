"""Summary, usage series, forecasts, anomalies and insights.

Everything here is a calculation over stored readings. The only labels that come from
outside are the model's own `is_estimate` flags, which are carried through untouched: a
number a user might act on must never be more confident than the data behind it.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

import pandas as pd
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from energy_ai import (
    ReadingRecord,
    build_report,
    compute_load_bands,
    daily_frame,
    forecast_energy,
    generate_insights,
    hourly_frame,
    to_frame,
)
from energy_ai.forecast import COMPLETE_DAY_COVERAGE

from backend.app.config import get_settings
from backend.app.models import Device, Reading
from backend.app.services import billing, tariffs
from backend.app.timestamps import iso, local_now, to_local, utc_now

# A day being reported on is only compared like-for-like once most of it is recorded.
COMPARABLE_COVERAGE = 0.8
ANALYSIS_WINDOW_DAYS = 120
DEFAULT_VIEW = "month"
VIEW_DAYS = {"day": 1, "week": 7, "month": 30}

NO_DEVICE = {
    "status": "no_device",
    "message": "No device has reported yet. Register a device or run the simulator to see figures.",
}


def _aware(value: datetime) -> datetime:
    """SQLite hands back naive datetimes; the rest of the app is timezone-aware."""
    from datetime import timezone as dt_timezone

    if value.tzinfo is None:
        return value.replace(tzinfo=dt_timezone.utc)
    return value


def pick_device(db: Session, device_id: str | None = None) -> Device | None:
    if device_id:
        return db.get(Device, device_id)
    return db.execute(
        select(Device).where(Device.is_primary.is_(True)).order_by(Device.id)
    ).scalars().first() or db.execute(select(Device).order_by(Device.id)).scalars().first()


def load_records(
    db: Session, device_id: str, start: datetime | None = None, end: datetime | None = None
) -> list[ReadingRecord]:
    query = select(Reading).where(Reading.device_id == device_id)
    if start is not None:
        query = query.where(Reading.ts >= start.replace(tzinfo=None))
    if end is not None:
        query = query.where(Reading.ts <= end.replace(tzinfo=None))
    query = query.order_by(Reading.ts)
    return [
        ReadingRecord(
            ts=_aware(row.ts),
            voltage=float(row.voltage or 0.0),
            current=float(row.current or 0.0),
            power_w=float(row.power_w or 0.0),
            energy_wh=float(row.energy_wh or 0.0),
        )
        for row in db.execute(query).scalars()
    ]


def build_frame(records: list[ReadingRecord]):
    return to_frame(records, get_tz())


def get_tz():
    from backend.app.timestamps import tz

    return tz()


# ---------------------------------------------------------------------------------------
# Usage series
# ---------------------------------------------------------------------------------------


def _range_days(view: str) -> int:
    if view not in VIEW_DAYS:
        raise ValueError("view must be one of: day, week, month")
    return VIEW_DAYS[view]


def _empty_usage() -> dict:
    return {
        **NO_DEVICE,
        "device": None,
        "range": None,
        "bucket": None,
        "buckets": [],
        "daily": [],
        "average_profile": [],
        "peaks": {},
        "totals": {
            "energy_kwh": None,
            "reading_count": 0,
            "avg_power_w": None,
            "peak_power_w": None,
            "night_kwh": None,
            "days_with_data": 0,
            "previous_energy_kwh": None,
            "previous_coverage_ratio": None,
            "change_percent": None,
            "comparison_note": "",
            "is_estimate": False,
        },
    }


def _empty_buckets(view: str) -> list[dict]:
    """Placeholder buckets so an empty window still draws a correctly labelled axis."""
    if view == "day":
        return [
            {
                "label": f"{hour:02d}:00",
                "start": f"{hour:02d}:00",
                "energy_kwh": 0.0,
                "avg_power_w": None,
                "peak_power_w": None,
                "reading_count": 0,
            }
            for hour in range(24)
        ]
    return []


def _coverage_note(range_days: int, previous_coverage: float, change: float | None) -> str:
    if change is None:
        return (
            f"Not compared with the previous {range_days} days: that window is only "
            f"{previous_coverage * 100:.0f}% covered, so a change figure would be misleading."
        )
    return f"Compared with the previous {range_days} days, which are {previous_coverage * 100:.0f}% covered."


def usage(db: Session, *, view: str = DEFAULT_VIEW, device_id: str | None = None) -> dict:
    view = (view or DEFAULT_VIEW).strip().lower()
    days = _range_days(view)

    device = pick_device(db, device_id)
    if device is None:
        return _empty_usage()

    tzinfo = get_tz()
    today = local_now().date()
    end = datetime.combine(today, datetime.min.time()).replace(tzinfo=tzinfo) + timedelta(days=1)
    start = end - timedelta(days=days)
    previous_start = start - timedelta(days=days)

    records = load_records(db, device.id, start=start.astimezone(utc_now().tzinfo), end=end.astimezone(utc_now().tzinfo))
    previous = load_records(
        db, device.id, start=previous_start.astimezone(utc_now().tzinfo), end=start.astimezone(utc_now().tzinfo)
    )

    frame = build_frame(records)
    if frame.empty:
        # An empty window is still a chart: it needs its axis, its bucket label, and an
        # honest zero, otherwise the UI cannot tell "no data" from "nothing wired up".
        payload = _empty_usage()
        payload["device"] = {"id": device.id, "name": device.name, "is_simulated": device.is_simulated}
        payload["data_label"] = data_label(db, device)
        payload["range"] = {
            "start": start.date().isoformat(),
            "end": (end - timedelta(days=1)).date().isoformat(),
            "days": days,
            "view": view,
        }
        payload["bucket"] = "Hour" if view == "day" else "Day"
        payload["buckets"] = _empty_buckets(view)
        return payload

    daily = daily_frame(frame, start=start.date(), end=(end - timedelta(days=1)).date())
    daily_rows = [_daily_row(stamp, row, tzinfo) for stamp, row in daily.iterrows()]
    daily_rows = [row for row in daily_rows if row["reading_count"] > 0 or row["coverage_ratio"] > 0]

    if view == "day":
        buckets = _hourly_buckets(frame, tzinfo)
    else:
        buckets = [
            {
                "label": row["date"],
                "start": row["date"],
                "energy_kwh": row["energy_kwh"],
                "night_kwh": row["night_kwh"],
                "avg_power_w": row["avg_power_w"],
                "peak_power_w": row["peak_power_w"],
                "coverage_ratio": row["coverage_ratio"],
            }
            for row in daily_rows
        ]

    previous_energy = sum(record.energy_wh for record in previous) / 1000.0 if previous else None
    previous_daily = _previous_coverage(previous, days)
    total_kwh = float(frame["energy_wh"].astype(float).sum() / 1000.0)
    change = None
    if previous_energy and previous_energy > 0 and previous_daily >= COMPARABLE_COVERAGE:
        change = round((total_kwh - previous_energy) / previous_energy * 100.0, 1)

    busiest = max(daily_rows, key=lambda row: row["energy_kwh"], default=None)

    return {
        "status": "ok",
        "device": {"id": device.id, "name": device.name, "is_simulated": device.is_simulated},
        "data_label": data_label(db, device),
        "range": {
            "start": start.date().isoformat(),
            "end": (end - timedelta(days=1)).date().isoformat(),
            "days": days,
            "view": view,
        },
        "bucket": "Hour" if view == "day" else "Day",
        "buckets": buckets,
        "daily": daily_rows,
        "average_profile": _average_profile(frame),
        "peaks": {
            "busiest_day": busiest["date"] if busiest else None,
            "busiest_day_kwh": busiest["energy_kwh"] if busiest else None,
            "quietest_day": min(daily_rows, key=lambda row: row["energy_kwh"])["date"] if daily_rows else None,
            "peak_power_w": float(frame["power_w"].max()),
        },
        "totals": {
            "energy_kwh": round(total_kwh, 3),
            "reading_count": int(len(records)),
            "avg_power_w": round(float(frame["power_w"].mean()), 2),
            "peak_power_w": round(float(frame["power_w"].max()), 2),
            "night_kwh": round(float(daily["night_kwh"].sum()), 3),
            "days_with_data": sum(1 for row in daily_rows if row["reading_count"]),
            "previous_energy_kwh": round(previous_energy, 3) if previous_energy is not None else None,
            "previous_coverage_ratio": round(previous_daily, 3),
            "change_percent": change,
            "comparison_note": _coverage_note(days, previous_daily, change),
            "is_estimate": False,
        },
    }


def _previous_coverage(records: list[ReadingRecord], days: int) -> float:
    if not records or days <= 0:
        return 0.0
    covered = {(to_local(record.ts).date()) for record in records}
    return min(len(covered) / days, 1.0)


def _daily_row(stamp: pd.Timestamp, row: pd.Series, tzinfo) -> dict:
    recorded = _num(row.get("sample_count", 0))
    return {
        "date": stamp.date().isoformat(),
        "energy_kwh": round(_num(row["energy_kwh"]), 3),
        "night_kwh": round(_num(row["night_kwh"]), 3),
        "deep_night_kwh": round(_num(row.get("deep_night_kwh", 0.0)), 3),
        "peak_window_kwh": round(_num(row.get("peak_window_kwh", 0.0)), 3),
        "offpeak_window_kwh": round(_num(row.get("offpeak_window_kwh", 0.0)), 3),
        "avg_power_w": round(_num(row["avg_power_w"]), 2) if recorded else None,
        "peak_power_w": round(_num(row["peak_power_w"]), 2) if recorded else None,
        "min_power_w": round(_num(row["min_power_w"]), 2) if recorded else None,
        "hours_above_high_draw": round(_num(row.get("hours_above_high_draw", 0.0)), 3),
        "reading_count": int(recorded),
        "coverage_ratio": round(_num(row.get("coverage_ratio", 0.0)), 3),
        "is_estimate": False,
    }


def _hourly_buckets(frame: pd.DataFrame, tzinfo) -> list[dict]:
    hourly = hourly_frame(frame)
    buckets = []
    for hour in range(24):
        selected = hourly[hourly.index.hour == hour]
        if selected.empty:
            buckets.append(
                {
                    "label": f"{hour:02d}:00",
                    "start": f"{hour:02d}:00",
                    "energy_kwh": 0.0,
                    "avg_power_w": None,
                    "peak_power_w": None,
                    "reading_count": 0,
                }
            )
            continue
        row = selected.iloc[0]
        # An hour with no readings is a reindexed row of NaN, not an absent row.
        recorded = _num(row["sample_count"])
        buckets.append(
            {
                "label": f"{hour:02d}:00",
                "start": f"{hour:02d}:00",
                "energy_kwh": round(_num(row["energy_kwh"]), 4),
                "avg_power_w": round(_num(row["avg_power_w"]), 2) if recorded else None,
                "peak_power_w": round(_num(row["peak_power_w"]), 2) if recorded else None,
                "reading_count": int(recorded),
            }
        )
    return buckets


def _average_profile(frame: pd.DataFrame) -> list[dict]:
    hourly = hourly_frame(frame)
    day_count = max(frame.index.normalize().nunique(), 1)
    profile = []
    for hour in range(24):
        selected = hourly[hourly.index.hour == hour]
        energy = float(selected["energy_kwh"].sum()) if not selected.empty else 0.0
        profile.append(
            {
                "hour": hour,
                "label": f"{hour:02d}:00",
                "energy_kwh": round(energy / day_count, 4),
                "avg_power_w": round(float(selected["avg_power_w"].mean()), 2) if not selected.empty else None,
            }
        )
    return profile


# ---------------------------------------------------------------------------------------
# Provenance
# ---------------------------------------------------------------------------------------


def data_label(db: Session, device: Device) -> str:
    """The banner text. A device that may be simulated gets the cautious label."""
    if device is not None and device.is_simulated:
        return "Simulated data"
    return "Measured data"


def provenance(db: Session, device: Device) -> dict:
    simulated = db.execute(
        select(func.count(Reading.id)).where(Reading.device_id == device.id, Reading.is_simulated.is_(True))
    ).scalar_one()
    hardware = db.execute(
        select(func.count(Reading.id)).where(Reading.device_id == device.id, Reading.is_simulated.is_(False))
    ).scalar_one()
    unknown = db.execute(
        select(func.count(Reading.id)).where(Reading.device_id == device.id, Reading.is_simulated.is_(None))
    ).scalar_one()

    fully_simulated = simulated > 0 and hardware == 0
    device_simulated = True if fully_simulated else device.is_simulated

    if fully_simulated:
        statement = (
            "No physical meter is attached to this install. Every reading was generated by the "
            "WattXpense simulator and is labelled as simulated throughout."
        )
    elif hardware == 0 and unknown > 0:
        statement = (
            "The source of these readings has not declared whether it is real hardware, so they are "
            "not labelled as measured."
        )
    elif simulated and hardware:
        statement = (
            f"Readings come from more than one source: {hardware} from hardware and {simulated} from the "
            "simulator. Simulated readings are labelled individually."
        )
    elif hardware:
        statement = "These readings came from a device that declared itself as hardware."
    else:
        statement = "No readings are stored for this device yet."

    return {
        "fully_simulated": fully_simulated,
        "readings_from_hardware": int(hardware),
        "readings_simulated": int(simulated),
        "readings_unlabelled": int(unknown),
        "device_is_simulated": bool(device_simulated),
        "statement": statement,
    }


# ---------------------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------------------


def _current_power(db: Session, device: Device) -> dict:
    row = db.execute(
        select(Reading).where(Reading.device_id == device.id).order_by(Reading.ts.desc())
    ).scalars().first()
    settings = get_settings()
    now = utc_now()
    if row is None:
        return {
            "latest_w": None,
            "latest_ts": None,
            "age_minutes": None,
            "is_stale": True,
            "timestamp_in_future": False,
            "clock_note": "",
            "is_estimate": False,
            "note": "No readings are stored for this device yet.",
        }

    stamp = _aware(row.ts)
    age_minutes = round((now - stamp).total_seconds() / 60.0, 1)
    in_future = stamp > now
    stale = in_future or age_minutes > settings.stale_reading_minutes

    clock_note = ""
    if in_future:
        clock_note = (
            "This reading is timestamped in the future, so the device clock is probably wrong. "
            "It is not treated as the freshest reading available."
        )
    elif stale:
        clock_note = f"The newest reading is {age_minutes:.0f} minutes old."

    return {
        "latest_w": round(float(row.power_w or 0.0), 2),
        "latest_ts": iso(stamp),
        "age_minutes": age_minutes,
        "is_stale": bool(stale),
        "timestamp_in_future": bool(in_future),
        "clock_note": clock_note,
        "voltage": round(float(row.voltage), 2) if row.voltage is not None else None,
        "current": round(float(row.current), 4) if row.current is not None else None,
        "is_estimate": False,
    }


def _change_vs_reference(
    current_kwh: float, current_coverage: float, reference_kwh: float | None, reference_coverage: float
) -> tuple[float | None, str]:
    """Never compare a part-recorded window with a complete one."""
    if current_coverage < COMPARABLE_COVERAGE:
        return None, (
            f"Today is only {current_coverage * 100:.0f}% recorded, so it is not compared with "
            "yesterday: a change figure against a complete day would be misleading."
        )
    if reference_kwh is None or reference_coverage < COMPARABLE_COVERAGE:
        return None, (
            f"Yesterday is only {reference_coverage * 100:.0f}% recorded, so today is not compared "
            "with it: a change figure would be misleading."
        )
    if reference_kwh <= 0:
        return None, "Yesterday recorded no energy, so there is nothing to compare today against."
    return round((current_kwh - reference_kwh) / reference_kwh * 100.0, 1), ""


def _num(value: Any, default: float = 0.0) -> float:
    """Read a number out of a reindexed day frame.

    A day with no readings is a real row of NaN rather than an absent row, so every read
    has to decide what "nothing recorded" means. For energy and counts that is zero; the
    caller passes a different default where the answer is genuinely unknown.
    """
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return default if number != number else number  # NaN != NaN


def _period_daily(db: Session, device: Device, start: date, end: date, config: tariffs.TariffConfig):
    tzinfo = get_tz()
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=tzinfo)
    end_dt = datetime.combine(end, datetime.min.time(), tzinfo=tzinfo) + timedelta(days=1)
    records = load_records(db, device.id, start=start_dt.astimezone(utc_now().tzinfo), end=end_dt.astimezone(utc_now().tzinfo))
    frame = build_frame(records)
    daily = daily_frame(
        frame,
        start=start,
        end=end,
        peak_start_hour=config.peak_start_hour,
        peak_end_hour=config.peak_end_hour,
    )
    return frame, daily


def summary(db: Session, *, device_id: str | None = None) -> dict:
    device = pick_device(db, device_id)
    if device is None:
        return {**NO_DEVICE, "generated_at": iso(utc_now())}

    settings = get_settings()
    config = tariffs.get_config(db)
    tzinfo = get_tz()
    today = local_now().date()

    # Today and yesterday on their own, for the part-period comparison.
    two_days_back = today - timedelta(days=1)
    _, recent = _period_daily(db, device, two_days_back, today, config)
    recent_map = {stamp.date().isoformat(): row for stamp, row in recent.iterrows()}

    today_row = recent_map.get(today.isoformat())
    yesterday_row = recent_map.get((today - timedelta(days=1)).isoformat())

    today_kwh = _num(today_row["energy_kwh"]) if today_row is not None else 0.0
    today_coverage = _num(today_row["coverage_ratio"]) if today_row is not None else 0.0
    yesterday_kwh = _num(yesterday_row["energy_kwh"]) if yesterday_row is not None else None
    yesterday_coverage = _num(yesterday_row["coverage_ratio"]) if yesterday_row is not None else 0.0
    change, change_note = _change_vs_reference(today_kwh, today_coverage, yesterday_kwh, yesterday_coverage)

    period_start, period_end = billing.period_bounds(today, config.billing_start_day)
    frame, daily = _period_daily(db, device, period_start, period_end, config)

    metered_kwh, metered_days, skipped_days, skipped_kwh = _metered_period(daily, today)
    today_day_cost = billing.marginal_day_cost(config, metered_kwh, today_kwh)

    forecast_daily = _run_forecast(daily, horizon_days=max((period_end - today).days + 1, 1), today=today)
    projected = _projected_bill(
        config=config,
        daily=daily,
        today=today,
        period_start=period_start,
        period_end=period_end,
        metered_kwh=metered_kwh,
        metered_days=metered_days,
        skipped_days=skipped_days,
        skipped_kwh=skipped_kwh,
        forecast=forecast_daily,
    )

    period_cost = billing.slab_cost(config, metered_kwh) if config.is_slab else None
    return {
        "status": "ok",
        "generated_at": iso(utc_now()),
        "device": {
            "id": device.id,
            "name": device.name,
            "location": device.location,
            "is_primary": device.is_primary,
            "is_simulated": device.is_simulated,
            "nominal_interval_seconds": device.nominal_interval_seconds,
        },
        "data_label": data_label(db, device),
        "data_provenance": provenance(db, device),
        "currency": config.currency,
        "timezone": settings.timezone_name,
        "now": iso(utc_now()),
        "current_power": _current_power(db, device),
        "energy_today": {
            "label": "Metered",
            "energy_kwh": round(today_kwh, 3),
            "night_kwh": round(_num(today_row["night_kwh"]), 3) if today_row is not None else 0.0,
            "peak_window_kwh": round(_num(today_row["peak_window_kwh"]), 3) if today_row is not None else 0.0,
            "offpeak_window_kwh": round(_num(today_row["offpeak_window_kwh"]), 3) if today_row is not None else 0.0,
            "reading_count": int(_num(today_row["sample_count"])) if today_row is not None else 0,
            "coverage_ratio": round(today_coverage, 3),
            "is_estimate": False,
            "change_vs_yesterday_percent": change,
            "change_note": change_note,
            "cost": today_day_cost["cost"],
            "cost_basis": today_day_cost["cost_basis"],
            "marginal_rate_per_kwh": today_day_cost["rate_per_kwh"],
            "cost_notes": today_day_cost["notes"],
        },
        "today_bill_detail": {
            "energy_kwh": round(today_kwh, 3),
            "energy_charge": today_day_cost["cost"],
            "standing_charge": 0.0,
            "tax": 0.0,
            "notes": today_day_cost["notes"],
        },
        "energy_this_period": {
            "energy_kwh": round(metered_kwh, 3),
            "cost": round(period_cost, 2) if period_cost is not None else None,
            "cost_basis": "progressive_slabs" if config.is_slab else None,
            "days_metered": metered_days,
            "days_skipped": len(skipped_days),
            "is_estimate": False,
        },
        "projected_bill": projected,
        "tariff": tariffs.as_dict(tariffs.get_active(db)),
        "meta": {"honesty": HONESTY_STATEMENT},
    }


HONESTY_STATEMENT = (
    "Simulated readings are labelled as simulated. Forecasts, projected bills, anomaly findings "
    "and savings estimates are models, not measurements, and are labelled as estimates."
)


def _metered_period(daily: pd.DataFrame, today: date) -> tuple[float, int, list[str], float]:
    """Only fully recorded days before today contribute to the projection.

    A day with a gap is excluded rather than scaled up, because scaling up invents energy
    that was never measured.
    """
    total = 0.0
    counted = 0
    skipped: list[str] = []
    skipped_kwh = 0.0
    for stamp, row in daily.iterrows():
        day = stamp.date()
        if day >= today:
            continue
        coverage = float(row.get("coverage_ratio", 0.0))
        energy = float(row["energy_kwh"])
        if coverage >= billing.METERED_DAY_COVERAGE:
            total += energy
            counted += 1
        else:
            skipped.append(day.isoformat())
            skipped_kwh += energy
    return round(total, 3), counted, skipped, round(skipped_kwh, 3)


def _run_forecast(daily: pd.DataFrame, *, horizon_days: int, today: date):
    return forecast_energy(
        daily,
        horizon_days=horizon_days,
        today=today,
        min_coverage=COMPLETE_DAY_COVERAGE,
    )


def _projected_bill(
    *,
    config: tariffs.TariffConfig,
    daily: pd.DataFrame,
    today: date,
    period_start: date,
    period_end: date,
    metered_kwh: float,
    metered_days: int,
    skipped_days: list[str],
    skipped_kwh: float,
    forecast,
) -> dict:
    days_total = (period_end - period_start).days + 1
    elapsed = billing.days_elapsed(period_start, today)
    forecast_days = max((period_end - today).days + 1, 1)

    forecast_ok = forecast.status == "ok" and forecast.predicted_energy_kwh > 0
    forecast_kwh = float(forecast.predicted_energy_kwh) if forecast_ok else 0.0
    covers_today = bool(forecast_ok)
    total_kwh = round(metered_kwh + forecast_kwh, 3)

    peak_kwh = offpeak_kwh = None
    if config.is_time_of_use:
        peak_kwh = round(float(daily["peak_window_kwh"].sum()), 3)
        offpeak_kwh = round(float(daily["offpeak_window_kwh"].sum()), 3)
        if forecast_ok and total_kwh > (peak_kwh + offpeak_kwh):
            share = (peak_kwh / (peak_kwh + offpeak_kwh)) if (peak_kwh + offpeak_kwh) > 0 else 0.0
            forecast_kwh_total = forecast_kwh
            peak_kwh = round(peak_kwh + forecast_kwh_total * share, 3)
            offpeak_kwh = round(offpeak_kwh + forecast_kwh_total * (1 - share), 3)

    estimate = billing.estimate_bill(
        config,
        energy_kwh=total_kwh,
        days=days_total,
        peak_kwh=peak_kwh,
        offpeak_kwh=offpeak_kwh,
        notes=[],
    )

    if covers_today:
        basis = (
            f"{metered_days} metered day(s) up to yesterday, plus {forecast_days} forecast day(s) "
            "from today to the end of the period. Today is forecast as a whole day, so it is never "
            "counted twice and can never be missing."
        )
    else:
        basis = (
            f"{metered_days} metered day(s) only. There is not enough history to forecast the "
            f"remaining {forecast_days} day(s), so they are not included and this figure is "
            "understated by whatever they will consume."
        )

    explanation = [
        f"This is a projection, not a bill, and an estimate rather than a measurement.",
        basis,
    ]
    if skipped_days:
        explanation.append(
            f"{len(skipped_days)} day(s) before today were too thinly recorded to count and were "
            f"left out rather than scaled up. That holds about {skipped_kwh:.2f} kWh of recorded "
            "energy back, so the projection is understated by that much."
        )
    if config.is_slab:
        explanation.append(
            "A slab bill depends on where the month's total lands, so this projection is more "
            "uncertain than a flat one would be if the month crosses a boundary differently."
        )
    explanation.append(f"The fixed charge is included for all {days_total} days of the period, including days still to come.")

    return {
        **estimate,
        "is_projection": True,
        "is_estimate": True,
        "period": {
            "start": period_start.isoformat(),
            "end_inclusive": period_end.isoformat(),
            "days_total": days_total,
            "days_elapsed": elapsed,
            "days_metered": metered_days,
            "days_skipped": skipped_days,
        },
        "energy": {
            **estimate["energy"],
            "total_kwh": total_kwh,
            "metered_kwh": round(metered_kwh, 3),
            "forecast_kwh": round(forecast_kwh, 3),
        },
        "forecast": {
            "status": forecast.status,
            "predicted_remaining_kwh": round(forecast_kwh, 3),
            "predicted_avg_kwh_per_day": round(forecast.predicted_avg_kwh_per_day, 3),
            "confidence": forecast.confidence,
            "confidence_reason": forecast.confidence_reason,
            "days_forecast": forecast_days if covers_today else 0,
        },
        "forecast_covers_today": covers_today,
        "projection_basis": basis,
        "understated_by_kwh": round(skipped_kwh, 3),
        "explanation": explanation,
    }


# ---------------------------------------------------------------------------------------
# AI panels
# ---------------------------------------------------------------------------------------


def _analysis_frame(db: Session, device: Device, config: tariffs.TariffConfig, days: int = ANALYSIS_WINDOW_DAYS):
    tzinfo = get_tz()
    today = local_now().date()
    start = today - timedelta(days=days - 1)
    start_dt = datetime.combine(start, datetime.min.time(), tzinfo=tzinfo)
    end_dt = datetime.combine(today, datetime.min.time(), tzinfo=tzinfo) + timedelta(days=1)
    records = load_records(db, device.id, start=start_dt.astimezone(utc_now().tzinfo), end=end_dt.astimezone(utc_now().tzinfo))
    frame = build_frame(records)
    daily = daily_frame(
        frame,
        start=start,
        end=today,
        peak_start_hour=config.peak_start_hour,
        peak_end_hour=config.peak_end_hour,
    )
    return frame, daily, today


def forecast_panel(db: Session, *, horizon_days: int = 7, device_id: str | None = None) -> dict:
    device = pick_device(db, device_id)
    if device is None:
        return {**NO_DEVICE, "is_estimate": True}
    config = tariffs.get_config(db)
    _, daily, today = _analysis_frame(db, device, config)
    result = _run_forecast(daily, horizon_days=horizon_days, today=today)
    payload = result.as_dict()
    payload["is_estimate"] = True
    return payload


def anomalies_panel(db: Session, *, device_id: str | None = None) -> dict:
    device = pick_device(db, device_id)
    if device is None:
        return {**NO_DEVICE, "is_estimate": True}
    settings = get_settings()
    config = tariffs.get_config(db)
    frame, daily, _ = _analysis_frame(db, device, config)
    report = build_report(
        daily,
        frame,
        nominal_v=settings.nominal_voltage_v,
        voltage_tolerance_percent=settings.voltage_tolerance_percent,
    )
    payload = report.as_dict()
    payload["is_estimate"] = True
    return payload


def insights_panel(db: Session, *, device_id: str | None = None) -> dict:
    device = pick_device(db, device_id)
    if device is None:
        return {**NO_DEVICE, "is_estimate": True}
    config = tariffs.get_config(db)
    frame, daily, _ = _analysis_frame(db, device, config)

    period_start, period_end = billing.period_bounds(local_now().date(), config.billing_start_day)
    _, period_daily = _period_daily(db, device, period_start, period_end, config)
    metered_kwh, _, _, _ = _metered_period(period_daily, local_now().date())

    cost = tariffs.cost_context(config, metered_kwh or None)
    report = generate_insights(daily, frame, cost)
    payload = report.as_dict()
    payload["is_estimate"] = True
    payload["load_bands"] = payload.get("load_bands") or [band.as_dict() for band in compute_load_bands(frame)]
    return payload
