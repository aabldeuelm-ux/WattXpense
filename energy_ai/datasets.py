from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Sequence

import numpy as np
import pandas as pd

NIGHT_START_HOUR = 23
NIGHT_END_HOUR = 6
DEEP_NIGHT_START_HOUR = 1
DEEP_NIGHT_END_HOUR = 5
HIGH_DRAW_W = 1000.0
DAILY_COLUMNS = (
    "energy_kwh",
    "avg_power_w",
    "peak_power_w",
    "min_power_w",
    "median_power_w",
    "night_kwh",
    "deep_night_kwh",
    "hours_above_high_draw",
    "sample_count",
    "coverage_ratio",
    "peak_window_kwh",
    "offpeak_window_kwh",
)


@dataclass(frozen=True)
class ReadingRecord:
    ts: datetime
    voltage: float
    current: float
    power_w: float
    energy_wh: float


def records_from_readings(readings: Sequence[Any]) -> list[ReadingRecord]:
    records: list[ReadingRecord] = []
    for item in readings:
        if isinstance(item, ReadingRecord):
            records.append(item)
            continue
        records.append(
            ReadingRecord(
                ts=item.ts,
                voltage=float(item.voltage),
                current=float(item.current),
                power_w=float(item.power_w),
                energy_wh=float(item.energy_wh),
            )
        )
    return records


def to_frame(records: Sequence[ReadingRecord], tz) -> pd.DataFrame:
    columns = ["ts", "voltage", "current", "power_w", "energy_wh"]
    if not records:
        empty = pd.DataFrame({name: [] for name in columns})
        empty.index = pd.DatetimeIndex([], tz=tz, name="ts")
        return empty

    frame = pd.DataFrame(
        {
            "ts": [r.ts for r in records],
            "voltage": [r.voltage for r in records],
            "current": [r.current for r in records],
            "power_w": [r.power_w for r in records],
            "energy_wh": [r.energy_wh for r in records],
        }
    ).set_index("ts")
    frame = frame.sort_index()
    frame.index = frame.index.tz_convert(tz)
    frame["local_hour"] = frame.index.hour
    return frame


def hourly_frame(frame: pd.DataFrame) -> pd.DataFrame:
    columns = ["energy_kwh", "avg_power_w", "peak_power_w", "min_power_w", "sample_count"]
    if frame.empty:
        return pd.DataFrame(columns=columns, index=pd.DatetimeIndex([], name="bucket_start"))
    work = frame.copy()
    work["energy_kwh"] = work["energy_wh"].astype(float) / 1000.0
    grouped = work.resample("h")
    return pd.DataFrame(
        {
            "energy_kwh": grouped["energy_kwh"].sum(),
            "avg_power_w": grouped["power_w"].mean(),
            "peak_power_w": grouped["power_w"].max(),
            "min_power_w": grouped["power_w"].min(),
            "sample_count": grouped["power_w"].size(),
        }
    )[columns]


def daily_frame(
    frame: pd.DataFrame,
    *,
    start: date | None = None,
    end: date | None = None,
    peak_start_hour: int | None = None,
    peak_end_hour: int | None = None,
) -> pd.DataFrame:
    if frame.empty:
        out = pd.DataFrame({name: [] for name in DAILY_COLUMNS})
        out.index = pd.DatetimeIndex([], name="date")
        return out

    work = frame.copy()
    work["energy_kwh"] = work["energy_wh"].astype(float) / 1000.0
    local_hour = work["local_hour"]
    is_night = (local_hour >= NIGHT_START_HOUR) | (local_hour < NIGHT_END_HOUR)
    is_deep_night = (local_hour >= DEEP_NIGHT_START_HOUR) & (local_hour < DEEP_NIGHT_END_HOUR)
    work["night_kwh"] = np.where(is_night, work["energy_kwh"], 0.0)
    work["deep_night_kwh"] = np.where(is_deep_night, work["energy_kwh"], 0.0)

    median_interval = work.index.to_series().diff().dt.total_seconds().median()
    interval_hours = float(median_interval) / 3600.0 if median_interval and not pd.isna(median_interval) else 0.0
    if not np.isfinite(interval_hours) or interval_hours <= 0:
        interval_hours = 1.0 / 60.0
    work["hours_above_high_draw"] = np.where(work["power_w"] >= HIGH_DRAW_W, interval_hours, 0.0)

    is_peak = _peak_mask(local_hour, peak_start_hour, peak_end_hour)
    work["peak_window_kwh"] = np.where(is_peak, work["energy_kwh"], 0.0)
    work["offpeak_window_kwh"] = np.where(~is_peak, work["energy_kwh"], 0.0)

    day_key = work.index.normalize()
    grouped = work.groupby(day_key)
    out = pd.DataFrame(
        {
            "energy_kwh": grouped["energy_kwh"].sum(),
            "avg_power_w": grouped["power_w"].mean(),
            "peak_power_w": grouped["power_w"].max(),
            "min_power_w": grouped["power_w"].min(),
            "median_power_w": grouped["power_w"].median(),
            "night_kwh": grouped["night_kwh"].sum(),
            "deep_night_kwh": grouped["deep_night_kwh"].sum(),
            "hours_above_high_draw": grouped["hours_above_high_draw"].sum(),
            "sample_count": grouped["power_w"].size(),
            "peak_window_kwh": grouped["peak_window_kwh"].sum(),
            "offpeak_window_kwh": grouped["offpeak_window_kwh"].sum(),
        }
    )

    out["span_hours"] = grouped.apply(
        lambda g: (g.index[-1] - g.index[0]).total_seconds() / 3600.0,
        include_groups=False,
    ) + interval_hours
    out["coverage_ratio"] = np.minimum(1.0, out["span_hours"] / 24.0)

    tz = out.index.tz
    full_index = pd.date_range(
        start=pd.Timestamp(start or out.index[0].date(), tz=tz),
        end=pd.Timestamp(end or out.index[-1].date(), tz=tz),
        freq="D",
    )
    out = out.reindex(full_index)[list(DAILY_COLUMNS)]
    out.index.name = "date"
    return out


def _peak_mask(local_hour, start_hour: int | None, end_hour: int | None) -> np.ndarray:
    if start_hour is None or end_hour is None or start_hour == end_hour:
        return np.zeros(len(local_hour), dtype=bool)
    hours = np.asarray(local_hour)
    if start_hour < end_hour:
        return (hours >= start_hour) & (hours < end_hour)
    return (hours >= start_hour) | (hours < end_hour)


def robust_median(values: pd.Series | np.ndarray) -> float:
    clean = pd.Series(values).replace([np.inf, -np.inf], np.nan).dropna()
    if clean.empty:
        return float("nan")
    return float(clean.median())


def robust_scale(values: pd.Series | np.ndarray) -> float:
    clean = pd.Series(values).replace([np.inf, -np.inf], np.nan).dropna()
    if len(clean) < 2:
        return float("nan")
    median = float(clean.median())
    mad = float((clean - median).abs().median())
    scaled = mad * 1.4826
    if scaled <= 0:
        return float(clean.std(ddof=1)) if len(clean) > 1 else float("nan")
    return scaled


def robust_z(value: float, median: float, scale: float) -> float:
    if not np.isfinite(scale) or scale <= 0:
        return float("nan")
    return float((value - median) / scale)


def pct_change(current: float, reference: float) -> float | None:
    if not np.isfinite(current) or not np.isfinite(reference) or reference == 0:
        return None
    return float((current - reference) / abs(reference) * 100.0)


def mape(actual: np.ndarray, predicted: np.ndarray) -> float:
    actual = np.asarray(actual, dtype=float)
    predicted = np.asarray(predicted, dtype=float)
    mask = np.isfinite(actual) & np.isfinite(predicted) & (np.abs(actual) > 1e-9)
    if not mask.any():
        return float("nan")
    return float(np.mean(np.abs((actual[mask] - predicted[mask]) / actual[mask])) * 100.0)


def local_days_back(today: date, days: int) -> date:
    return today - timedelta(days=days)
