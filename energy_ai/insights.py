from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from energy_ai.datasets import (
    DEEP_NIGHT_END_HOUR,
    DEEP_NIGHT_START_HOUR,
    HIGH_DRAW_W,
    NIGHT_END_HOUR,
    NIGHT_START_HOUR,
    pct_change,
    robust_median,
    robust_scale,
)

BASELINE_REDUCTION_ASSUMPTION = 0.25
DEFERRABLE_PEAK_SHARE = 0.30
DAYS_PER_MONTH = 30
BAND_EDGES = (100.0, 300.0, 1000.0, 2500.0)
BAND_LABELS = (
    "Minimal draw (under 100 W)",
    "Low draw (100-300 W)",
    "Medium draw (300-1,000 W)",
    "High draw (1,000-2,500 W)",
    "Very high draw (2,500 W and above)",
)


@dataclass(frozen=True)
class CostContext:
    currency: str
    effective_rate_per_kwh: float
    peak_rate_per_kwh: float | None = None
    offpeak_rate_per_kwh: float | None = None
    rate_basis: str = "flat_rate"

    @property
    def has_time_of_use(self) -> bool:
        return self.peak_rate_per_kwh is not None and self.offpeak_rate_per_kwh is not None

    @property
    def has_slab(self) -> bool:
        return self.rate_basis.startswith(("blended_across_slabs", "highest_slab"))

    @property
    def rate_caveat(self) -> str:
        """Wording that keeps a blended slab rate from being read as a fixed per-unit price."""
        if self.rate_basis == "blended_across_slabs":
            return (
                f"{self.effective_rate_per_kwh} {self.currency}/kWh is the average across the slabs of this "
                "bill, not a fixed per-unit price: each unit is charged at the rate of the slab it falls in, "
                "so the average changes as consumption grows."
            )
        if self.rate_basis == "highest_slab":
            return (
                f"{self.effective_rate_per_kwh} {self.currency}/kWh is the highest rate in this slab tariff, "
                "not a fixed per-unit price: lower units are charged at lower slab rates."
            )
        return ""

    def cost(self, kwh: float) -> float:
        return round(float(kwh) * float(self.effective_rate_per_kwh), 2)


@dataclass
class Insight:
    id: str
    title: str
    category: str
    priority: str
    summary: str
    evidence: list[str] = field(default_factory=list)
    how_it_is_calculated: str = ""
    savings_kwh_per_month: float | None = None
    savings_cost_per_month: float | None = None
    savings_basis: str = ""
    assumptions: list[str] = field(default_factory=list)
    confidence: str = "moderate"
    method: str = ""
    is_estimate: bool = True

    def as_dict(self) -> dict:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, float) and value is not None:
                payload[key] = round(value, 3) if np.isfinite(value) else None
        return payload


@dataclass
class LoadBand:
    label: str
    lower_w: float
    upper_w: float | None
    sample_share_percent: float
    energy_share_percent: float
    energy_kwh: float
    is_estimate: bool = True

    def as_dict(self) -> dict:
        return {
            "label": self.label,
            "lower_w": self.lower_w,
            "upper_w": self.upper_w,
            "sample_share_percent": round(self.sample_share_percent, 2),
            "energy_share_percent": round(self.energy_share_percent, 2),
            "energy_kwh": round(self.energy_kwh, 3),
            "is_estimate": True,
        }


@dataclass
class InsightReport:
    generated_at: str
    status: str
    days_analysed: int
    total_energy_kwh: float
    insights: list[Insight] = field(default_factory=list)
    load_bands: list[LoadBand] = field(default_factory=list)
    savings_range_kwh_per_month: tuple[float, float] = (0.0, 0.0)
    savings_range_cost_per_month: tuple[float, float] = (0.0, 0.0)
    currency: str = "USD"
    notes: list[str] = field(default_factory=list)
    is_estimate: bool = True

    def as_dict(self) -> dict:
        return {
            "generated_at": self.generated_at,
            "status": self.status,
            "is_estimate": True,
            "days_analysed": self.days_analysed,
            "total_energy_kwh": round(self.total_energy_kwh, 3),
            "insights": [i.as_dict() for i in self.insights],
            "load_bands": [b.as_dict() for b in self.load_bands],
            "savings_range_kwh_per_month": [
                round(self.savings_range_kwh_per_month[0], 3),
                round(self.savings_range_kwh_per_month[1], 3),
            ],
            "savings_range_cost_per_month": [
                round(self.savings_range_cost_per_month[0], 2),
                round(self.savings_range_cost_per_month[1], 2),
            ],
            "currency": self.currency,
            "notes": self.notes,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _kwh_between(frame: pd.DataFrame, start_hour: int, end_hour: int) -> float:
    if frame.empty:
        return 0.0
    hours = frame["local_hour"]
    mask = (hours >= start_hour) & (hours < end_hour)
    return float(frame.loc[mask, "energy_wh"].astype(float).sum() / 1000.0)


def _insight_baseline_load(daily: pd.DataFrame, frame: pd.DataFrame, cost: CostContext) -> Insight | None:
    if frame.empty or "energy_wh" not in frame:
        return None

    hours = frame["local_hour"]
    deep_night = frame[(hours >= DEEP_NIGHT_START_HOUR) & (hours < DEEP_NIGHT_END_HOUR)]
    if len(deep_night) < 12:
        return None

    baseline_w = robust_median(deep_night["power_w"])
    floor_w = float(np.percentile(frame["power_w"].astype(float).dropna(), 5)) if frame["power_w"].notna().any() else float("nan")
    if not np.isfinite(baseline_w):
        return None

    total_kwh = float(frame["energy_wh"].astype(float).sum() / 1000.0)
    days = max((frame.index[-1].normalize() - frame.index[0].normalize()).days + 1, 1)
    monthly_kwh = total_kwh / days * DAYS_PER_MONTH
    baseline_monthly_kwh = baseline_w / 1000.0 * 24 * DAYS_PER_MONTH
    baseline_share = baseline_monthly_kwh / monthly_kwh * 100.0 if monthly_kwh > 0 else float("nan")

    saving_kwh = baseline_monthly_kwh * BASELINE_REDUCTION_ASSUMPTION
    saving_cost = cost.cost(saving_kwh)

    return Insight(
        id="sustained_baseline_load",
        title="A baseline load never switches off",
        category="standby",
        priority="high" if baseline_share > 15 else "medium",
        summary=(
            f"Between {DEEP_NIGHT_START_HOUR:02d}:00 and {DEEP_NIGHT_END_HOUR:02d}:00 the median draw is "
            f"{baseline_w:.0f} W. Nothing in the house turns that off, so it runs all day. That baseline alone is "
            f"about {baseline_monthly_kwh:.0f} kWh a month, roughly {baseline_share:.0f}% of your total use."
        ),
        evidence=[
            f"Overnight median draw: {baseline_w:.0f} W (from {len(deep_night)} readings).",
            f"5th-percentile draw across all readings: {floor_w:.0f} W, which is the effective floor of the household load.",
            f"Extrapolated monthly cost of the baseline at {cost.effective_rate_per_kwh} {cost.currency}/kWh: "
            f"{cost.cost(baseline_monthly_kwh)} {cost.currency}.",
        ],
        how_it_is_calculated=(
            f"baseline kWh/month = {baseline_w:.0f} W / 1000 x 24 h x {DAYS_PER_MONTH} days = "
            f"{baseline_monthly_kwh:.1f} kWh"
        ),
        savings_kwh_per_month=saving_kwh,
        savings_cost_per_month=saving_cost,
        savings_basis=(
            f"Assumes you cut the always-on baseline by {BASELINE_REDUCTION_ASSUMPTION * 100:.0f}% "
            f"(switching off or putting standby devices on smart plugs): {baseline_monthly_kwh:.1f} kWh x "
            f"{BASELINE_REDUCTION_ASSUMPTION:.2f} = {saving_kwh:.1f} kWh, then x {cost.effective_rate_per_kwh} "
            f"{cost.currency}/kWh = {saving_cost} {cost.currency}/month"
        ),
        assumptions=[
            f"A {BASELINE_REDUCTION_ASSUMPTION * 100:.0f}% reduction in always-on draw is achievable. This is a "
            "planning figure, not a measurement, and could be higher or lower.",
            "Your usage pattern stays similar to the period analysed.",
            *([cost.rate_caveat] if cost.has_slab else []),
        ],
        confidence="moderate" if len(deep_night) >= 50 else "low",
        method="median power between 01:00 and 05:00 local time, extrapolated to a full month",
    )


def _insight_night_share(daily: pd.DataFrame, frame: pd.DataFrame) -> Insight | None:
    if frame.empty:
        return None
    total_kwh = float(frame["energy_wh"].astype(float).sum() / 1000.0)
    if total_kwh <= 0:
        return None
    night_kwh = _kwh_between(frame, NIGHT_START_HOUR, 24) + _kwh_between(frame, 0, NIGHT_END_HOUR)
    share = night_kwh / total_kwh * 100.0
    if share < 12:
        return None

    return Insight(
        id="night_time_share",
        title="A large share of your energy is used overnight",
        category="standby",
        priority="high" if share > 25 else "medium",
        summary=(
            f"{share:.0f}% of the energy in this period ({night_kwh:.1f} kWh of {total_kwh:.1f} kWh) was used "
            f"between {NIGHT_START_HOUR:02d}:00 and {NIGHT_END_HOUR:02d}:00. High overnight use usually means "
            "standby loads, drying appliances or heating on a timer rather than genuine need."
        ),
        evidence=[
            f"Overnight energy: {night_kwh:.2f} kWh.",
            f"Total energy in period: {total_kwh:.2f} kWh.",
        ],
        how_it_is_calculated=(
            f"overnight share = energy between {NIGHT_START_HOUR:02d}:00 and {NIGHT_END_HOUR:02d}:00 "
            f"({night_kwh:.2f} kWh) / total energy ({total_kwh:.2f} kWh) x 100"
        ),
        confidence="moderate",
        method="share of metered energy by local hour of day",
    )


def _insight_peak_shifting(daily: pd.DataFrame, cost: CostContext) -> Insight | None:
    if daily is None or daily.empty or not cost.has_time_of_use:
        return None
    if "peak_window_kwh" not in daily or "offpeak_window_kwh" not in daily:
        return None

    peak_kwh = float(daily["peak_window_kwh"].dropna().sum())
    offpeak_kwh = float(daily["offpeak_window_kwh"].dropna().sum())
    total = peak_kwh + offpeak_kwh
    if total <= 0 or peak_kwh <= 0:
        return None

    days = max(int(daily["peak_window_kwh"].notna().sum()), 1)
    peak_rate = float(cost.peak_rate_per_kwh)
    offpeak_rate = float(cost.offpeak_rate_per_kwh)
    difference = peak_rate - offpeak_rate
    if difference <= 0:
        return None

    peak_month_kwh = peak_kwh / days * DAYS_PER_MONTH
    deferrable_kwh = peak_month_kwh * DEFERRABLE_PEAK_SHARE
    saving_kwh_equivalent = deferrable_kwh * difference / (peak_rate if peak_rate else 1.0)
    saving_cost = round(deferrable_kwh * difference, 2)

    return Insight(
        id="peak_shifting",
        title="Shift flexible loads out of the peak window",
        category="tariff",
        priority="high" if peak_kwh / total > 0.3 else "medium",
        summary=(
            f"{peak_kwh / total * 100:.0f}% of your energy ({peak_kwh:.1f} kWh over {days} days) falls inside the "
            f"peak window at {peak_rate} {cost.currency}/kWh, against {offpeak_rate} {cost.currency}/kWh off-peak. "
            f"Moving flexible loads like laundry, dishwashers or EV charging out of that window is worth about "
            f"{saving_cost} {cost.currency} a month."
        ),
        evidence=[
            f"Peak-window energy: {peak_kwh:.2f} kWh across {days} day(s).",
            f"Off-peak energy: {offpeak_kwh:.2f} kWh.",
            f"Rate difference: {peak_rate} - {offpeak_rate} = {difference} {cost.currency}/kWh.",
        ],
        how_it_is_calculated=(
            f"peak kWh/month = {peak_kwh:.2f} / {days} x {DAYS_PER_MONTH} = {peak_month_kwh:.1f} kWh; "
            f"deferrable = {peak_month_kwh:.1f} x {DEFERRABLE_PEAK_SHARE:.2f} = {deferrable_kwh:.1f} kWh; "
            f"saving = {deferrable_kwh:.1f} x {difference} = {saving_cost} {cost.currency}"
        ),
        savings_kwh_per_month=saving_kwh_equivalent,
        savings_cost_per_month=saving_cost,
        savings_basis=(
            f"Assumes {DEFERRABLE_PEAK_SHARE * 100:.0f}% of peak-window energy is deferrable to off-peak hours and "
            f"that you keep using the same amount of energy, just at a cheaper time"
        ),
        assumptions=[
            f"Only {DEFERRABLE_PEAK_SHARE * 100:.0f}% of peak usage can realistically be moved. The rest is "
            "heating, lighting or use that genuinely happens then.",
            "Your tariff peak window stays the same. Check it before acting.",
        ],
        confidence="moderate",
        method="measured peak-window energy x configured rate difference",
    )


def _insight_weekday_vs_weekend(daily: pd.DataFrame) -> Insight | None:
    if daily is None or daily.empty:
        return None
    series = daily["energy_kwh"].dropna()
    if len(series) < 14:
        return None

    weekday = series[series.index.dayofweek < 5]
    weekend = series[series.index.dayofweek >= 5]
    if len(weekday) < 5 or len(weekend) < 2:
        return None

    weekday_mean = float(weekday.mean())
    weekend_mean = float(weekend.mean())
    change = pct_change(weekend_mean, weekday_mean)
    if change is None or abs(change) < 8:
        return None

    return Insight(
        id="weekday_vs_weekend",
        title="Your weekends use considerably less energy than weekdays",
        category="behaviour",
        priority="low",
        summary=(
            f"Weekday days average {weekday_mean:.1f} kWh while weekend days average {weekend_mean:.1f} kWh, a "
            f"{abs(change):.0f}% {'drop' if change < 0 else 'rise'}. The pattern is consistent enough to suggest "
            "the home is mostly empty on weekdays."
        ),
        evidence=[
            f"Weekday average: {weekday_mean:.2f} kWh over {len(weekday)} day(s).",
            f"Weekend average: {weekend_mean:.2f} kWh over {len(weekend)} day(s).",
        ],
        how_it_is_calculated=(
            "mean daily energy grouped by day of week, then (weekend mean - weekday mean) / weekday mean x 100"
        ),
        confidence="moderate",
        method="descriptive comparison of daily totals by day of week",
    )


def _insight_recent_trend(daily: pd.DataFrame) -> Insight | None:
    if daily is None or daily.empty:
        return None
    series = daily["energy_kwh"].dropna()
    if len(series) < 16:
        return None

    recent = series.tail(7)
    previous = series.tail(14).head(7)
    if len(recent) < 5 or len(previous) < 5:
        return None

    recent_mean = float(recent.mean())
    previous_mean = float(previous.mean())
    change = pct_change(recent_mean, previous_mean)
    if change is None or abs(change) < 10:
        return None

    projected = recent_mean * DAYS_PER_MONTH
    return Insight(
        id="recent_trend",
        title="Your usage is trending up" if change > 0 else "Your usage is trending down",
        category="trend",
        priority="medium",
        summary=(
            f"The last 7 days average {recent_mean:.1f} kWh against {previous_mean:.1f} kWh for the 7 days before, "
            f"a {abs(change):.0f}% {'increase' if change > 0 else 'decrease'}. Held at this level, a 30-day month "
            f"would use about {projected:.0f} kWh."
        ),
        evidence=[
            f"Last 7 days: {recent_mean:.2f} kWh/day average.",
            f"Previous 7 days: {previous_mean:.2f} kWh/day average.",
        ],
        how_it_is_calculated=(
            "(recent 7-day mean - previous 7-day mean) / previous 7-day mean x 100; "
            f"projection = {recent_mean:.2f} x {DAYS_PER_MONTH} days"
        ),
        confidence="low",
        method="comparison of two consecutive 7-day windows",
    )


def _insight_high_draw_hours(daily: pd.DataFrame) -> Insight | None:
    if daily is None or daily.empty or "hours_above_high_draw" not in daily:
        return None
    series = daily["hours_above_high_draw"].dropna()
    if len(series) < 7:
        return None

    mean_hours = float(series.mean())
    if mean_hours < 1.0:
        return None

    return Insight(
        id="high_draw_hours",
        title="You run high-power loads for several hours a day",
        category="behaviour",
        priority="medium",
        summary=(
            f"On average the load sits above {HIGH_DRAW_W:.0f} W for {mean_hours:.1f} hours a day. Above 1 kW you are "
            "usually running ovens, kettles, washing machines, showers or space heating, and those are the loads "
            "worth batching or shifting."
        ),
        evidence=[f"Mean time above {HIGH_DRAW_W:.0f} W: {mean_hours:.1f} h/day over {len(series)} day(s)."],
        how_it_is_calculated="mean of daily hours where instantaneous power exceeded 1,000 W",
        confidence="moderate",
        method="count of samples above 1,000 W converted to hours per day",
    )


def _insight_coverage(daily: pd.DataFrame) -> Insight | None:
    if daily is None or daily.empty or "coverage_ratio" not in daily:
        return None
    coverage = daily["coverage_ratio"]
    thin = int((coverage < 0.75).sum())
    missing = int(daily["energy_kwh"].isna().sum())
    if thin == 0 and missing == 0:
        return None

    share = (thin + missing) / max(len(coverage), 1) * 100.0
    return Insight(
        id="data_coverage",
        title="Part of your history is missing, which weakens the estimates",
        category="data_quality",
        priority="medium",
        summary=(
            f"{thin} day(s) had less than 75% reading coverage and {missing} day(s) have no readings at all "
            f"({share:.0f}% of the period). Consumption totals for those days are too low and the forecast is less "
            "reliable while the gaps remain."
        ),
        evidence=[
            f"Days with thin coverage: {thin}.",
            f"Days with no readings: {missing}.",
            f"Total days in window: {len(coverage)}.",
        ],
        how_it_is_calculated="count of days where the time between the first and last reading is under 75% of 24 hours",
        confidence="high",
        method="coverage ratio per day compared against a 75% threshold",
    )


def compute_load_bands(frame: pd.DataFrame) -> list[LoadBand]:
    if frame is None or frame.empty or "power_w" not in frame:
        return []
    power = frame["power_w"].astype(float)
    energy_kwh = frame["energy_wh"].astype(float) / 1000.0
    total_samples = int(power.notna().sum())
    total_kwh = float(energy_kwh.sum())
    if total_samples == 0 or total_kwh <= 0:
        return []

    bands: list[LoadBand] = []
    lower = 0.0
    for index, label in enumerate(BAND_LABELS):
        upper = BAND_EDGES[index] if index < len(BAND_EDGES) else None
        if upper is None:
            mask = power >= lower
        else:
            mask = (power >= lower) & (power < upper)
        samples = int(mask.sum())
        kwh = float(energy_kwh[mask].sum())
        bands.append(
            LoadBand(
                label=label,
                lower_w=lower,
                upper_w=upper,
                sample_share_percent=samples / total_samples * 100.0,
                energy_share_percent=kwh / total_kwh * 100.0,
                energy_kwh=kwh,
            )
        )
        lower = upper if upper is not None else lower
    return bands


def generate_insights(daily: pd.DataFrame, frame: pd.DataFrame, cost: CostContext) -> InsightReport:
    generated_at = _now_iso()

    if daily is None or daily.empty or frame is None or frame.empty:
        return InsightReport(
            generated_at=generated_at,
            status="insufficient_data",
            days_analysed=0,
            total_energy_kwh=0.0,
            currency=cost.currency,
            notes=["No readings are stored yet, so there is nothing to analyse. Start the simulator and check back."],
        )

    total_kwh = float(frame["energy_wh"].astype(float).sum() / 1000.0)
    days = max((frame.index[-1].normalize() - frame.index[0].normalize()).days + 1, 1)

    candidates: list[Insight | None] = [
        _insight_baseline_load(daily, frame, cost),
        _insight_night_share(daily, frame),
        _insight_peak_shifting(daily, cost),
        _insight_high_draw_hours(daily),
        _insight_recent_trend(daily),
        _insight_weekday_vs_weekend(daily),
        _insight_coverage(daily),
    ]
    insights = [item for item in candidates if item is not None]
    priority_order = {"high": 0, "medium": 1, "low": 2}
    insights.sort(key=lambda i: (priority_order.get(i.priority, 3), i.id))

    saving_items = [i for i in insights if i.savings_kwh_per_month is not None]
    kwh_low = min((i.savings_kwh_per_month or 0.0 for i in saving_items), default=0.0)
    kwh_high = sum((i.savings_kwh_per_month or 0.0 for i in saving_items))
    cost_low = min((i.savings_cost_per_month or 0.0 for i in saving_items), default=0.0)
    cost_high = sum((i.savings_cost_per_month or 0.0 for i in saving_items))

    notes = [
        "Savings figures are planning estimates built from the arithmetic shown on each card. They are not measured "
        "savings and they do not add up to a guarantee.",
        "The lowest figure comes from the single most promising change; the highest adds every change together, which "
        "assumes you act on all of them at once.",
        "Readings come from a whole circuit, so WattXpense cannot tell you which appliance used the energy. Anything "
        "appliance-specific needs a per-socket measurement.",
    ]

    return InsightReport(
        generated_at=generated_at,
        status="ok",
        days_analysed=days,
        total_energy_kwh=total_kwh,
        insights=insights,
        load_bands=compute_load_bands(frame),
        savings_range_kwh_per_month=(kwh_low, kwh_high),
        savings_range_cost_per_month=(cost_low, cost_high),
        currency=cost.currency,
        notes=notes,
    )
