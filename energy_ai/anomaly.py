from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import RobustScaler

from energy_ai.datasets import robust_median, robust_scale, robust_z

ML_MIN_DAYS = 20
MAD_FALLBACK_MIN_DAYS = 8
SUSTAINED_MIN_MINUTES = 30
HIGH_Z = 6.0
MODERATE_Z = 4.0
DETAIL_DAYS = 30


@dataclass
class AnomalyFinding:
    id: str
    kind: str
    severity: str
    title: str
    reason: str
    evidence: list[str] = field(default_factory=list)
    possible_causes: list[str] = field(default_factory=list)
    causes_are_hypotheses: bool = True
    window_start: str | None = None
    window_end: str | None = None
    metric_value: float | None = None
    reference_value: float | None = None
    deviation_percent: float | None = None
    score: float | None = None
    is_estimate: bool = True
    method: str = ""

    def as_dict(self) -> dict:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, float) and value is not None and np.isfinite(value):
                payload[key] = round(value, 3)
            elif isinstance(value, float) and value is not None:
                payload[key] = None
        return payload


@dataclass
class SustainedLoad:
    start: str
    end: str
    duration_minutes: float
    avg_power_w: float
    peak_power_w: float
    baseline_power_w: float
    threshold_w: float
    excess_kwh: float
    sample_count: int
    is_estimate: bool = True

    def as_dict(self) -> dict:
        payload = asdict(self)
        for key, value in list(payload.items()):
            if isinstance(value, float):
                payload[key] = round(value, 3)
        return payload


@dataclass
class AnomalyReport:
    status: str
    method: str
    generated_at: str
    days_analysed: int
    findings: list[AnomalyFinding] = field(default_factory=list)
    sustained_loads: list[SustainedLoad] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    is_estimate: bool = True
    references: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "method": self.method,
            "generated_at": self.generated_at,
            "is_estimate": True,
            "days_analysed": self.days_analysed,
            "findings": [f.as_dict() for f in self.findings],
            "sustained_loads": [s.as_dict() for s in self.sustained_loads],
            "notes": self.notes,
            "references": self.references,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def _severity_from_z(z: float) -> str:
    if not np.isfinite(z):
        return "low"
    if abs(z) >= HIGH_Z:
        return "high"
    if abs(z) >= MODERATE_Z:
        return "moderate"
    return "low"


def _fmt(value: float, places: int = 2) -> str:
    return f"{value:,.{places}f}"


def _causes_for_high_energy(coverage_ratio: float) -> list[str]:
    causes = [
        "A high-power appliance ran for longer than usual (for example heating, cooling or a clothes dryer).",
        "A schedule or timer changed, such as heating running while the property is empty.",
        "A relay, thermostat or valve is stuck, keeping a circuit energised.",
        "Extra occupancy, such as guests or working from home.",
    ]
    if coverage_ratio < 0.75:
        causes.insert(0, "A data gap rather than real extra consumption: this day has thin reading coverage.")
    return causes


def _causes_for_low_energy(coverage_ratio: float) -> list[str]:
    causes = [
        "The property was genuinely unoccupied, such as a holiday.",
        "Main loads were broken, switched off at the consumer unit, or the meter was bypassed.",
    ]
    if coverage_ratio < 0.75:
        causes.insert(0, "A data gap rather than real low consumption: this day has thin reading coverage.")
    return causes


def detect_daily_anomalies(daily: pd.DataFrame) -> AnomalyReport:
    generated_at = _now_iso()

    if daily is None or daily.empty or "energy_kwh" not in daily:
        return AnomalyReport(
            status="insufficient_data",
            method="none",
            generated_at=generated_at,
            days_analysed=0,
            notes=["No readings are stored yet, so there is nothing to compare against."],
            references=[],
        )

    complete = daily.dropna(subset=["energy_kwh"])
    days = int(len(complete))
    if days < MAD_FALLBACK_MIN_DAYS:
        return AnomalyReport(
            status="insufficient_data",
            method="none",
            generated_at=generated_at,
            days_analysed=days,
            notes=[
                f"Day-level anomaly detection needs at least {MAD_FALLBACK_MIN_DAYS} complete days; {days} available."
            ],
            references=[],
        )

    window = complete.tail(DETAIL_DAYS) if days > DETAIL_DAYS else complete
    reference = complete if days > DETAIL_DAYS else complete
    ref_days = int(len(reference))

    feature_columns = [
        c
        for c in ("energy_kwh", "avg_power_w", "peak_power_w", "night_kwh", "hours_above_high_draw")
        if c in window.columns
    ]

    if days >= ML_MIN_DAYS and len(feature_columns) >= 2:
        method = (
            f"IsolationForest (unsupervised, contamination='auto') on {', '.join(feature_columns)}, "
            f"fitted on the last {ref_days} days and scored on the most recent {len(window)} days"
        )
        anomalies_mask = _isolation_forest_mask(reference, window, feature_columns)
    else:
        method = (
            f"robust z-score (median +/- {MODERATE_Z:.0f} scaled median absolute deviations) on daily energy, "
            "because there is not enough history to train the IsolationForest"
        )
        median = robust_median(reference["energy_kwh"])
        scale = robust_scale(reference["energy_kwh"])
        z_values = (window["energy_kwh"] - median) / scale if scale and np.isfinite(scale) else pd.Series(0.0, index=window.index)
        anomalies_mask = z_values.abs() >= MODERATE_Z

    flagged = window[anomalies_mask]
    if "coverage_ratio" in flagged.columns:
        # A day with thin coverage will look low simply because most of it was never recorded.
        # That is a data problem, reported separately, so it must not also be called a usage anomaly.
        flagged = flagged[flagged["coverage_ratio"] >= 0.75]
    reference_columns = (
        "energy_kwh",
        "peak_power_w",
        "night_kwh",
        "hours_above_high_draw",
        "avg_power_w",
    )
    medians = {
        col: (robust_median(reference[col]) if col in reference.columns else float("nan"))
        for col in reference_columns
    }

    # A quiet Sunday in a mostly-weekday record is normal, not an anomaly. Comparing each day
    # against the median of the same weekday removes that whole class of false alarm, provided
    # there are enough weeks of history to make that median meaningful.
    weekday_reference: dict[int, pd.DataFrame] = {}
    if "coverage_ratio" in reference.columns:
        usable = reference[reference["coverage_ratio"] >= 0.75]
    else:
        usable = reference
    for weekday in range(7):
        same_day = usable[usable.index.dayofweek == weekday]
        if len(same_day) >= 3:
            weekday_reference[weekday] = same_day

    def column_value(row: pd.Series, name: str) -> float:
        if name not in row:
            return float("nan")
        value = row.get(name, float("nan"))
        try:
            return float(value)
        except (TypeError, ValueError):
            return float("nan")

    def baseline_for(day: pd.Timestamp) -> tuple[pd.DataFrame, str]:
        same_day = weekday_reference.get(day.dayofweek)
        if same_day is not None:
            return same_day, f"median of {len(same_day)} previous {day.strftime('%A')}s"
        return reference, f"{ref_days}-day median"

    def weekday_median(frame: pd.DataFrame, column: str) -> float:
        if column not in frame.columns:
            return float("nan")
        return float(robust_median(frame[column]))

    findings: list[AnomalyFinding] = []
    for day, row in flagged.sort_values("energy_kwh", ascending=False).iterrows():
        energy = column_value(row, "energy_kwh")
        coverage_value = column_value(row, "coverage_ratio")
        coverage = coverage_value if np.isfinite(coverage_value) else 1.0
        baseline_frame, baseline_label = baseline_for(pd.Timestamp(day))
        baseline = weekday_median(baseline_frame, "energy_kwh")
        baseline_scale = robust_scale(baseline_frame["energy_kwh"])
        z = robust_z(energy, baseline, baseline_scale)
        ratio = energy / baseline if baseline else float("nan")

        peak = column_value(row, "peak_power_w")
        peak_ref = weekday_median(baseline_frame, "peak_power_w")
        high_draw = column_value(row, "hours_above_high_draw")
        high_draw_ref = weekday_median(baseline_frame, "hours_above_high_draw")

        # The unsupervised detector is deliberately trigger-happy, and it also fires on ordinary
        # weekday/weekend variation that is normal for a home. So a day is only reported as
        # unusual when there is corroborating evidence beyond the total alone: either the energy
        # itself sits far outside the normal spread, or the load profile is out of band. A heater
        # left on for three hours moves the peak and the high-draw hours, not just the total,
        # so this keeps genuine faults while dropping routine variation.
        energy_outlier = np.isfinite(z) and abs(z) >= MODERATE_Z
        peak_outlier = np.isfinite(peak) and np.isfinite(peak_ref) and peak_ref > 0 and peak >= peak_ref * 1.4
        draw_outlier = (
            np.isfinite(high_draw)
            and np.isfinite(high_draw_ref)
            and high_draw >= high_draw_ref + 1.5
        )
        corroborated = energy_outlier or peak_outlier or draw_outlier
        if not corroborated:
            continue

        severity = _severity_from_z(z)
        direction = "high" if energy >= baseline else "low"
        if not energy_outlier:
            # Nothing is wrong with the daily total, so the headline must not claim the total
            # was unusual. Report the load profile instead, capped below the energy severity.
            severity = "moderate"

        evidence = [
            f"Daily energy {_fmt(energy)} kWh against the {_fmt(baseline)} kWh {baseline_label} ({ratio:.2f}x)."
        ]
        if energy_outlier and np.isfinite(z):
            evidence.append(f"Robust z-score is {z:+.1f} (a score above 4 is unusual, above 6 is extreme).")
        elif np.isfinite(z):
            evidence.append(
                f"Robust z-score is only {z:+.1f}, so the daily total is within the normal spread. "
                "It is the shape of the load that stands out."
            )

        if np.isfinite(peak) and np.isfinite(peak_ref) and peak_ref > 0 and peak > peak_ref * 1.4:
            evidence.append(
                f"Peak draw reached {_fmt(peak, 0)} W against a typical {_fmt(peak_ref, 0)} W."
            )

        if np.isfinite(high_draw) and np.isfinite(high_draw_ref) and high_draw > high_draw_ref + 1.0:
            evidence.append(
                f"Time spent above the high-draw threshold was {_fmt(high_draw, 1)} h versus a typical "
                f"{_fmt(high_draw_ref, 1)} h."
            )

        night = column_value(row, "night_kwh")
        if np.isfinite(night) and medians["night_kwh"] and night > medians["night_kwh"] * 2 + 0.3:
            evidence.append(
                f"Night-time energy between 23:00 and 06:00 was {_fmt(night)} kWh versus a typical "
                f"{_fmt(medians['night_kwh'])} kWh, which points to a load running while the home is idle."
            )

        if coverage < 0.75:
            evidence.append(
                f"Reading coverage for this day is only {coverage * 100:.0f}%, so part of the difference may be missing data."
            )

        if direction == "high":
            headline = f"Unusually high energy use on {day.date().isoformat()}"
            causes = _causes_for_high_energy(coverage)
        else:
            headline = f"Unusually low energy use on {day.date().isoformat()}"
            causes = _causes_for_low_energy(coverage)
        if not energy_outlier:
            headline = f"Unusual load pattern on {day.date().isoformat()}"
            causes = _causes_for_high_energy(coverage)

        reason = (
            f"{_fmt(energy)} kWh on {day.date().isoformat()} is {ratio:.2f}x the {baseline_label} of "
            f"{_fmt(baseline)} kWh. " + (" ".join(evidence[1:3]) if len(evidence) > 1 else "")
        ).strip()
        if not energy_outlier:
            reason = (
                f"Total use on {day.date().isoformat()} is normal for a {day.strftime('%A')} at "
                f"{_fmt(energy)} kWh ({ratio:.2f}x the {baseline_label} of {_fmt(baseline)} kWh), but the "
                "load profile is out of band. " + " ".join(evidence[1:3])
            ).strip()

        findings.append(
            AnomalyFinding(
                id=f"daily-{day.date().isoformat()}",
                kind="daily_energy",
                severity=severity,
                title=headline,
                reason=reason,
                evidence=evidence,
                possible_causes=causes,
                window_start=f"{day.date().isoformat()}T00:00:00",
                window_end=f"{day.date().isoformat()}T23:59:59",
                metric_value=energy,
                reference_value=baseline,
                deviation_percent=(ratio - 1.0) * 100.0,
                score=float(z) if np.isfinite(z) else None,
                method=method,
            )
        )

    notes = [
        "Anomalies are ranked against the recent median, not against a fixed threshold, so they adapt as your usage changes.",
        "Each day is compared with previous occurrences of the same weekday where there is enough history, so a "
        "consistently quiet weekend is not reported as unusual.",
        "Possible causes are hypotheses for you to check. They are not diagnoses, and the readings cannot identify which appliance was involved.",
    ]
    if days < ML_MIN_DAYS:
        notes.append(
            f"The IsolationForest needs {ML_MIN_DAYS}+ days, so this report used a simple robust z-score instead. "
            "Collect more history to switch to the machine learning detector."
        )

    return AnomalyReport(
        status="ok",
        method=method,
        generated_at=generated_at,
        days_analysed=days,
        findings=findings,
        notes=notes,
        references=[
            "Liu, F.T., Ting, K.M. and Zhou, Z.H. (2008), 'Isolation Forest', 2008 IEEE International Conference on Data Mining, 413-422.",
            "Iglewicz, B. and Hoaglin, D.C. (1993), 'How to Detect and Handle Outliers', ASQC Quality Press.",
        ],
    )


def _isolation_forest_mask(reference: pd.DataFrame, window: pd.DataFrame, feature_columns: list[str]) -> pd.Series:
    scaler = RobustScaler()
    x_reference = scaler.fit_transform(reference[feature_columns].to_numpy(dtype=float))
    x_reference = np.nan_to_num(x_reference, nan=0.0, posinf=0.0, neginf=0.0)

    model = IsolationForest(contamination="auto", random_state=42, n_estimators=200)
    model.fit(x_reference)

    x_window = scaler.transform(window[feature_columns].to_numpy(dtype=float))
    x_window = np.nan_to_num(x_window, nan=0.0, posinf=0.0, neginf=0.0)
    labels = model.predict(x_window)
    return pd.Series(labels == -1, index=window.index)


def detect_sustained_loads(
    frame: pd.DataFrame,
    *,
    min_minutes: float = SUSTAINED_MIN_MINUTES,
    max_findings: int = 8,
) -> list[SustainedLoad]:
    if frame is None or frame.empty or "power_w" not in frame:
        return []

    power = frame["power_w"].astype(float)
    if power.notna().sum() < 3:
        return []

    baseline = robust_median(power)
    scale = robust_scale(power)
    if not np.isfinite(baseline):
        return []
    if not np.isfinite(scale) or scale <= 0:
        quantiles = power.quantile([0.75, 0.25]).to_numpy(dtype=float)
        scale = float((quantiles[0] - quantiles[1]) / 1.349) if np.isfinite(quantiles).all() else 1.0
    threshold = baseline + 3.0 * max(scale, 1.0)

    index = power.index
    above = power.to_numpy(dtype=float) > threshold
    if not above.any():
        return []

    values = power.to_numpy(dtype=float)
    deltas = index.to_series().diff().dt.total_seconds()
    median_step = float(np.nanmedian(deltas.to_numpy(dtype=float))) if len(deltas) else 60.0
    if not np.isfinite(median_step) or median_step <= 0:
        median_step = 60.0
    step_hours = median_step / 3600.0

    loads: list[SustainedLoad] = []
    start_idx: int | None = None
    for position in range(len(values) + 1):
        is_above = position < len(values) and above[position]
        if is_above and start_idx is None:
            start_idx = position
        elif not is_above and start_idx is not None:
            segment_length = position - start_idx
            segment_power = values[start_idx:position]
            duration_min = segment_length * median_step / 60.0
            if duration_min >= min_minutes:
                excess_w = np.maximum(segment_power - baseline, 0.0)
                loads.append(
                    SustainedLoad(
                        start=index[start_idx].isoformat(),
                        end=index[position - 1].isoformat(),
                        duration_minutes=duration_min,
                        avg_power_w=float(np.mean(segment_power)),
                        peak_power_w=float(np.max(segment_power)),
                        baseline_power_w=float(baseline),
                        threshold_w=float(threshold),
                        excess_kwh=float(excess_w.sum() * step_hours / 1000.0),
                        sample_count=int(segment_length),
                    )
                )
            start_idx = None

    loads.sort(key=lambda item: item.excess_kwh, reverse=True)
    return loads[:max_findings]


def detect_voltage_issues(
    frame: pd.DataFrame,
    *,
    nominal_v: float,
    tolerance_percent: float,
) -> list[AnomalyFinding]:
    if frame is None or frame.empty or "voltage" not in frame:
        return []

    voltage = frame["voltage"].astype(float).dropna()
    if len(voltage) < 5:
        return []

    low_limit = nominal_v * (1 - tolerance_percent / 100.0)
    high_limit = nominal_v * (1 + tolerance_percent / 100.0)
    mean_voltage = float(voltage.mean())
    out_of_band = int(((voltage < low_limit) | (voltage > high_limit)).sum())
    findings: list[AnomalyFinding] = []

    if out_of_band == 0:
        return findings

    share = out_of_band / len(voltage) * 100.0
    severity = "high" if share > 10 else ("moderate" if share > 3 else "low")
    deviation = (mean_voltage - nominal_v) / nominal_v * 100.0
    findings.append(
        AnomalyFinding(
            id="voltage-band",
            kind="voltage",
            severity=severity,
            title="Supply voltage outside the expected band",
            reason=(
                f"{out_of_band} of {len(voltage)} readings ({share:.1f}%) fell outside "
                f"{low_limit:.0f}-{high_limit:.0f} V, which is +/-{tolerance_percent:.0f}% of the nominal "
                f"{nominal_v:.0f} V. Mean voltage was {mean_voltage:.1f} V ({deviation:+.1f}%)."
            ),
            evidence=[
                f"Out-of-band readings: {out_of_band} of {len(voltage)}.",
                f"Observed minimum {voltage.min():.1f} V and maximum {voltage.max():.1f} V.",
            ],
            possible_causes=[
                "Mains supply variation from the utility network.",
                "Undervoltage caused by a heavy load on the same supply, common in older wiring.",
                "A sensor or current-transformer scaling error rather than a genuine supply problem.",
            ],
            metric_value=mean_voltage,
            reference_value=nominal_v,
            deviation_percent=deviation,
            method=f"threshold check against nominal {nominal_v:.0f} V +/-{tolerance_percent:.0f}%",
        )
    )
    return findings


def detect_data_gaps(daily: pd.DataFrame, *, coverage_threshold: float = 0.75) -> list[AnomalyFinding]:
    if daily is None or daily.empty:
        return []
    findings: list[AnomalyFinding] = []

    observed_days = [
        day
        for day in daily.index
        if "energy_kwh" in daily and bool(np.isfinite(daily.at[day, "energy_kwh"]))
    ]
    if not observed_days:
        return findings
    first_observed, last_observed = observed_days[0], observed_days[-1]

    missing = daily[daily["energy_kwh"].isna()]
    for day in missing.index:
        if not first_observed <= day <= last_observed:
            continue
        findings.append(
            AnomalyFinding(
                id=f"gap-{day.date().isoformat()}",
                kind="data_gap",
                severity="moderate",
                title=f"No readings stored for {day.date().isoformat()}",
                reason="This day has no energy readings at all, so it is missing from consumption totals and forecasts treat it as a gap.",
                evidence=["Zero readings recorded for the day."],
                possible_causes=[
                    "The simulator, meter or MQTT bridge was not running.",
                    "A network outage stopped the device from reporting.",
                    "The device was powered off or being reconfigured.",
                ],
                window_start=f"{day.date().isoformat()}T00:00:00",
                window_end=f"{day.date().isoformat()}T23:59:59",
                method="completeness check on the daily index",
            )
        )

    if "coverage_ratio" in daily:
        thin = daily[(daily["coverage_ratio"] < coverage_threshold) & daily["energy_kwh"].notna()]
        for day, row in thin.iterrows():
            findings.append(
                AnomalyFinding(
                    id=f"thin-coverage-{day.date().isoformat()}",
                    kind="data_gap",
                    severity="low",
                    title=f"Thin reading coverage on {day.date().isoformat()}",
                    reason=(
                        f"Readings span about {float(row['coverage_ratio']) * 100:.0f}% of the day, so the daily "
                        "total understates real consumption and the forecast treats this day as partially observed."
                    ),
                    evidence=[f"Sample count for the day: {int(row.get('sample_count', 0))}."],
                    possible_causes=[
                        "The device reported intermittently because of Wi-Fi signal loss.",
                        "The sampling interval was increased to save battery or data.",
                    ],
                    window_start=f"{day.date().isoformat()}T00:00:00",
                    window_end=f"{day.date().isoformat()}T23:59:59",
                    metric_value=float(row["coverage_ratio"]) * 100.0,
                    reference_value=coverage_threshold * 100.0,
                    method=f"coverage below {coverage_threshold * 100:.0f}% of 24 h",
                )
            )
    return findings


def build_report(
    daily: pd.DataFrame,
    frame: pd.DataFrame,
    *,
    nominal_v: float,
    voltage_tolerance_percent: float,
) -> AnomalyReport:
    report = detect_daily_anomalies(daily)
    if report.status != "ok":
        return report

    report.sustained_loads = detect_sustained_loads(frame)
    report.findings.extend(detect_voltage_issues(frame, nominal_v=nominal_v, tolerance_percent=voltage_tolerance_percent))
    report.findings.extend(detect_data_gaps(daily))

    severity_order = {"high": 0, "moderate": 1, "low": 2}
    report.findings.sort(key=lambda f: (severity_order.get(f.severity, 3), f.id))

    if report.sustained_loads:
        total_excess = sum(load.excess_kwh for load in report.sustained_loads)
        biggest = report.sustained_loads[0]
        report.notes.append(
            f"The {len(report.sustained_loads)} longest high-draw window(s) add about {total_excess:.2f} kWh above "
            f"the {biggest.baseline_power_w:.0f} W baseline. The biggest ran from {biggest.start[11:16]} to "
            f"{biggest.end[11:16]} at an average of {biggest.avg_power_w:.0f} W."
        )
    return report
