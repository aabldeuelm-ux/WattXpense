from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import numpy as np
import pandas as pd
from sklearn.ensemble import HistGradientBoostingRegressor

from energy_ai.datasets import mape

# A day counts as complete only if it was actually measured for at least this fraction of the
# day. Anything less is treated as a part-day and kept out of both training and billing bases.
COMPLETE_DAY_COVERAGE = 0.9

MIN_HISTORY_DAYS = 7
ML_MIN_HISTORY_DAYS = 21
FEATURE_WARMUP_DAYS = 14
BAND_SIGMA = 1.28
LOW_CONFIDENCE_MAPE = 25.0
GOOD_CONFIDENCE_MAPE = 12.0

Predictor = Callable[[pd.Series], float]


@dataclass
class ForecastMetrics:
    mae_kwh: float | None
    rmse_kwh: float | None
    mape_percent: float | None
    r2: float | None
    baseline_mape_percent: float | None
    skill_vs_baseline_percent: float | None
    test_samples: int
    test_window_days: int

    def as_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in asdict(self).items()}


@dataclass
class ForecastPoint:
    date: str
    energy_kwh: float
    lower_kwh: float
    upper_kwh: float
    kind: str = "forecast"

    def as_dict(self) -> dict:
        return {
            "date": self.date,
            "energy_kwh": round(self.energy_kwh, 3),
            "lower_kwh": round(self.lower_kwh, 3),
            "upper_kwh": round(self.upper_kwh, 3),
            "kind": self.kind,
        }


@dataclass
class DailyForecast:
    status: str
    method: str
    model: str
    generated_at: str
    horizon_days: int
    history_days: int
    first_day_forecast: str
    last_day_forecast: str
    metrics: ForecastMetrics | None
    points: list[ForecastPoint] = field(default_factory=list)
    predicted_energy_kwh: float = 0.0
    predicted_avg_kwh_per_day: float = 0.0
    peak_share: float | None = None
    predicted_peak_kwh: float = 0.0
    predicted_offpeak_kwh: float = 0.0
    confidence: str = "low"
    confidence_reason: str = ""
    band_note: str = ""
    excluded_incomplete_days: int = 0
    last_complete_day: str = ""
    warnings: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    is_estimate: bool = True

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "method": self.method,
            "model": self.model,
            "generated_at": self.generated_at,
            "is_estimate": True,
            "horizon_days": self.horizon_days,
            "history_days": self.history_days,
            "first_day_forecast": self.first_day_forecast,
            "last_day_forecast": self.last_day_forecast,
            "metrics": self.metrics.as_dict() if self.metrics else None,
            "points": [p.as_dict() for p in self.points],
            "predicted_energy_kwh": round(self.predicted_energy_kwh, 3),
            "predicted_avg_kwh_per_day": round(self.predicted_avg_kwh_per_day, 3),
            "peak_share": round(self.peak_share, 4) if self.peak_share is not None else None,
            "predicted_peak_kwh": round(self.predicted_peak_kwh, 3),
            "predicted_offpeak_kwh": round(self.predicted_offpeak_kwh, 3),
            "confidence": self.confidence,
            "confidence_reason": self.confidence_reason,
            "band_note": self.band_note,
            "excluded_incomplete_days": self.excluded_incomplete_days,
            "last_complete_day": self.last_complete_day,
            "warnings": self.warnings,
            "notes": self.notes,
        }


def _now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_features(series: pd.Series) -> pd.DataFrame:
    index = series.index
    values = series.astype(float)
    filled = values.ffill()

    day_of_week = index.dayofweek.to_numpy()
    day_of_year = index.dayofyear.to_numpy()

    features = pd.DataFrame(index=index)
    features["t"] = np.arange(len(index), dtype=float)
    features["dow_sin"] = np.sin(2 * np.pi * day_of_week / 7.0)
    features["dow_cos"] = np.cos(2 * np.pi * day_of_week / 7.0)
    features["doy_sin"] = np.sin(2 * np.pi * day_of_year / 365.25)
    features["doy_cos"] = np.cos(2 * np.pi * day_of_year / 365.25)
    features["is_weekend"] = (day_of_week >= 5).astype(float)
    features["lag_1"] = filled.shift(1)
    features["lag_7"] = filled.shift(7)
    features["lag_14"] = filled.shift(14)
    features["roll_mean_7"] = filled.shift(1).rolling(7, min_periods=2).mean()
    features["roll_mean_14"] = filled.shift(1).rolling(14, min_periods=2).mean()
    features["recent_level"] = filled.shift(1).rolling(3, min_periods=1).mean()
    return features


def make_ml_predictor(series: pd.Series) -> tuple[Predictor, HistGradientBoostingRegressor, int]:
    features = build_features(series)
    target = series.astype(float)
    train_mask = target.notna().to_numpy()
    usable = int(train_mask.sum())

    x_train = features.to_numpy(dtype=float)[train_mask]
    y_train = target.to_numpy(dtype=float)[train_mask]
    fill_value = float(np.nanmedian(y_train)) if usable else 0.0
    x_train = np.nan_to_num(x_train, nan=fill_value, posinf=fill_value, neginf=fill_value)

    model = HistGradientBoostingRegressor(
        max_iter=300,
        learning_rate=0.06,
        max_depth=3,
        min_samples_leaf=2,
        l2_regularization=1.0,
        early_stopping=False,
        random_state=42,
    )
    model.fit(x_train, y_train)

    def predict_next(history: pd.Series) -> float:
        next_day = history.index[-1] + timedelta(days=1)
        extended = pd.concat([history.astype(float), pd.Series([np.nan], index=pd.DatetimeIndex([next_day]))])
        row = build_features(extended).loc[next_day].to_numpy(dtype=float).reshape(1, -1)
        row = np.nan_to_num(row, nan=fill_value, posinf=fill_value, neginf=fill_value)
        return max(float(model.predict(row)[0]), 0.0)

    return predict_next, model, usable


def build_ml_predictor(series: pd.Series) -> Predictor:
    return make_ml_predictor(series)[0]


def make_day_of_week_predictor(series: pd.Series) -> Predictor:
    def predict_next(history: pd.Series) -> float:
        next_day = history.index[-1] + timedelta(days=1)
        same_weekday = history[history.index.dayofweek == next_day.dayofweek].dropna()
        if not same_weekday.empty:
            return max(float(same_weekday.iloc[-3:].mean()), 0.0)
        fallback = history.dropna()
        if fallback.empty:
            return 0.0
        return max(float(fallback.iloc[-7:].mean()), 0.0)

    return predict_next


def make_lag7_baseline() -> Predictor:
    def predict_next(history: pd.Series) -> float:
        values = history.astype(float)
        if len(values) >= 8:
            value = values.iloc[-8]
            if np.isfinite(value):
                return max(float(value), 0.0)
        fallback = values.dropna()
        if fallback.empty:
            return 0.0
        return max(float(fallback.iloc[-7:].mean()), 0.0)

    return predict_next


def recursive_predict(
    predictor: Predictor,
    series: pd.Series,
    horizon: int,
    start_date: pd.Timestamp | None = None,
) -> list[tuple[pd.Timestamp, float]]:
    """Roll the model forward `horizon` days, optionally beginning on a specific date.

    The billing projection needs the forecast to start on a particular day so that it butts up
    against the metered days without overlapping them, which may be later than the day after
    the last complete reading when the data has a gap.
    """
    history = series.astype(float)
    results: list[tuple[pd.Timestamp, float]] = []
    next_day = history.index[-1] + timedelta(days=1)
    if start_date is not None:
        next_day = pd.Timestamp(start_date)
    for _ in range(horizon):
        prediction = float(predictor(history))
        prediction = max(prediction, 0.0) if np.isfinite(prediction) else 0.0
        history = pd.concat([history, pd.Series([prediction], index=pd.DatetimeIndex([next_day]))])
        results.append((next_day, prediction))
        next_day = next_day + timedelta(days=1)
    return results


def _validate(
    build_predictor: Callable[[pd.Series], Predictor],
    series: pd.Series,
    test_days: int,
) -> tuple[ForecastMetrics | None, list[float]]:
    if test_days < 1:
        return None, []

    train = series.iloc[:-test_days]
    test = series.iloc[-test_days:]
    if int(train.notna().sum()) < FEATURE_WARMUP_DAYS + 3:
        return None, []

    predictor = build_predictor(train)
    baseline = make_lag7_baseline()

    predicted: list[float] = []
    baseline_predicted: list[float] = []
    history = train.astype(float)
    for day, actual in test.items():
        value = float(predictor(history))
        value = max(value, 0.0) if np.isfinite(value) else 0.0
        predicted.append(value)
        baseline_predicted.append(baseline(history))
        history = pd.concat([history, pd.Series([value], index=pd.DatetimeIndex([day]))])

    actual_values = test.to_numpy(dtype=float)
    valid = np.isfinite(actual_values)
    if valid.sum() < 2:
        return None, predicted

    a = actual_values[valid]
    p = np.asarray(predicted, dtype=float)[valid]
    b = np.asarray(baseline_predicted, dtype=float)[valid]

    errors = p - a
    model_mape = mape(a, p)
    baseline_mape = mape(a, b)
    variance = float(np.var(a))
    r2 = float("nan") if variance <= 0 else float(1.0 - np.sum(errors**2) / (variance * len(a)))

    skill: float | None = None
    if np.isfinite(model_mape) and np.isfinite(baseline_mape) and baseline_mape > 1e-9:
        skill = float((baseline_mape - model_mape) / baseline_mape * 100.0)

    metrics = ForecastMetrics(
        mae_kwh=float(np.mean(np.abs(errors))),
        rmse_kwh=float(np.sqrt(np.mean(errors**2))),
        mape_percent=model_mape,
        r2=r2,
        baseline_mape_percent=baseline_mape,
        skill_vs_baseline_percent=skill,
        test_samples=int(valid.sum()),
        test_window_days=test_days,
    )
    return metrics, predicted


def _residual_sigma(metrics: ForecastMetrics | None, fallback: pd.Series) -> float:
    if metrics and metrics.rmse_kwh and np.isfinite(metrics.rmse_kwh):
        return max(float(metrics.rmse_kwh), 1e-6)
    clean = fallback.dropna()
    if len(clean) >= 2:
        return max(float(np.std(clean.to_numpy(dtype=float), ddof=1)), 1e-6)
    return 1e-6


def _choose_confidence(metrics: ForecastMetrics | None, history_days: int, used_ml: bool) -> tuple[str, str]:
    if not used_ml:
        return "low", "Not enough history to train the machine learning model, so this is a weekday-average estimate."
    if metrics is None:
        return "low", "There was not enough history to hold back a validation window, so accuracy could not be measured."
    if metrics.test_samples < 5:
        return "low", f"Only {metrics.test_samples} validation day(s) were available to test accuracy."
    if metrics.skill_vs_baseline_percent is not None and metrics.skill_vs_baseline_percent <= 0:
        return "low", (
            "The model did not beat a simple 'same day last week' rule on the validation window, "
            "so treat the numbers as a weekday-average estimate."
        )
    if (
        metrics.mape_percent is not None
        and metrics.mape_percent < GOOD_CONFIDENCE_MAPE
        and history_days >= 45
        and (metrics.skill_vs_baseline_percent or 0) > 25
    ):
        return "high", f"Validation error was {metrics.mape_percent:.1f}% and the model beat the simple baseline by {metrics.skill_vs_baseline_percent:.0f}%."
    if metrics.mape_percent is not None and metrics.mape_percent < LOW_CONFIDENCE_MAPE:
        return "moderate", f"Validation error was {metrics.mape_percent:.1f}% average percentage error on held-out days."
    return "low", f"Validation error was {metrics.mape_percent:.0f}% average percentage error, which is too high to trust closely." if metrics.mape_percent is not None else "Accuracy could not be established."


def forecast_energy(
    daily: pd.DataFrame,
    *,
    horizon_days: int = 7,
    peak_share: float | None = None,
    today: date | None = None,
    min_coverage: float = COMPLETE_DAY_COVERAGE,
    start_date: date | None = None,
) -> DailyForecast:
    horizon_days = max(1, int(horizon_days))
    generated_at = _now_iso()

    if daily is None or daily.empty or "energy_kwh" not in daily:
        return DailyForecast(
            status="insufficient_data",
            method="none",
            model="none",
            generated_at=generated_at,
            horizon_days=horizon_days,
            history_days=0,
            first_day_forecast="",
            last_day_forecast="",
            metrics=None,
            confidence="low",
            confidence_reason="No readings are stored yet, so no forecast can be produced.",
            notes=["Record or simulate some readings first; forecasting starts once a few days of history exist."],
        )

    raw_series = daily["energy_kwh"].astype(float)

    coverage = daily["coverage_ratio"].astype(float) if "coverage_ratio" in daily else None
    if coverage is not None:
        complete_mask = (coverage >= min_coverage) & raw_series.notna()
    else:
        complete_mask = raw_series.notna()

    series = raw_series.where(complete_mask)
    observed = series.dropna()

    excluded_incomplete = int(raw_series.notna().sum() - observed.shape[0])
    notes_tail: list[str] = []

    if observed.empty:
        return DailyForecast(
            status="insufficient_data",
            method="none",
            model="none",
            generated_at=generated_at,
            horizon_days=horizon_days,
            history_days=0,
            first_day_forecast="",
            last_day_forecast="",
            metrics=None,
            confidence="low",
            confidence_reason=(
                f"No day has complete reading coverage yet. At least one day above {min_coverage * 100:.0f}% "
                "coverage is needed before a daily total can be forecast."
            ),
            notes=[
                "Day-ahead forecasting needs whole days. The current day is still in progress, so wait for it to "
                "finish or check back tomorrow."
            ],
        )

    last_complete_day = observed.index[-1]
    series = series.loc[:last_complete_day]
    observed_days = int(series.notna().sum())

    if observed_days < MIN_HISTORY_DAYS:
        return DailyForecast(
            status="insufficient_data",
            method="none",
            model="none",
            generated_at=generated_at,
            horizon_days=horizon_days,
            history_days=observed_days,
            first_day_forecast="",
            last_day_forecast="",
            metrics=None,
            excluded_incomplete_days=excluded_incomplete,
            confidence="low",
            confidence_reason=(
                f"Forecasting needs at least {MIN_HISTORY_DAYS} complete days of history; {observed_days} available."
            ),
            notes=[
                "This is a cold start, not a failure. Keep the simulator or your meter running and the "
                "forecast switches on automatically."
            ],
        )

    usable_rows = observed_days
    used_ml = usable_rows >= max(ML_MIN_HISTORY_DAYS, FEATURE_WARMUP_DAYS + 3)

    if used_ml:
        test_days = int(min(7, max(3, usable_rows // 4)))
        metrics, _ = _validate(build_ml_predictor, series, test_days)
        predictor, model, _ = make_ml_predictor(series)
        method = "scikit-learn HistGradientBoostingRegressor on calendar + lag features"
        model_name = f"HistGradientBoostingRegressor(max_iter=300, max_depth=3, random_state=42), trained on {usable_rows} daily totals"
    else:
        test_days = int(min(5, max(2, usable_rows // 4)))
        metrics, _ = _validate(make_day_of_week_predictor, series, test_days)
        predictor = make_day_of_week_predictor(series)
        model_name = "weekday-average fallback (no machine learning model was trained)"
        method = "day-of-week mean of observed daily energy"

    anchor = pd.Timestamp(start_date).tz_localize(series.index.tz) if start_date is not None else None
    predictions = recursive_predict(predictor, series, horizon_days, start_date=anchor)
    sigma = _residual_sigma(metrics, series)
    half_width = BAND_SIGMA * sigma

    points: list[ForecastPoint] = []
    for day, value in predictions:
        points.append(
            ForecastPoint(
                date=day.date().isoformat(),
                energy_kwh=value,
                lower_kwh=max(value - half_width, 0.0),
                upper_kwh=value + half_width,
            )
        )

    total = float(sum(p.energy_kwh for p in points))
    warnings: list[str] = []
    if not used_ml:
        warnings.append(
            f"Only {observed_days} days of history are stored. That is below the {ML_MIN_HISTORY_DAYS}-day "
            "minimum for training the machine learning model, so a weekday average is used instead."
        )
    if metrics is None:
        warnings.append("Accuracy could not be measured because there was no room for a validation window.")
    else:
        if metrics.mape_percent is not None and not np.isfinite(metrics.mape_percent):
            warnings.append("Average percentage error could not be computed because some actual days were zero.")
        if metrics.skill_vs_baseline_percent is not None and metrics.skill_vs_baseline_percent <= 0:
            warnings.append(
                f"The model matched or lost to a simple 'same weekday last week' rule "
                f"(model {metrics.mape_percent:.1f}% error vs rule {metrics.baseline_mape_percent:.1f}% error). "
                "The forecast is therefore only as good as a calendar average."
            )

    coverage_all = daily["coverage_ratio"].dropna() if "coverage_ratio" in daily else pd.Series(dtype=float)
    if not coverage_all.empty and float((coverage_all < 0.75).mean()) > 0.2:
        warnings.append(
            f"{float((coverage_all < 0.75).mean()) * 100:.0f}% of days have less than 75% reading coverage, "
            "which lowers forecast accuracy."
        )
    if excluded_incomplete:
        notes_tail.append(
            f"{excluded_incomplete} day(s) were left out of the model because they were not fully covered, "
            f"including the day in progress. Only days at or above {min_coverage * 100:.0f}% coverage are treated "
            "as complete."
        )

    confidence, reason = _choose_confidence(metrics, observed_days, used_ml)

    share = peak_share if peak_share is not None else None
    predicted_peak = total * share if share is not None else 0.0
    predicted_offpeak = total - predicted_peak if share is not None else total

    notes = [
        "Validation is out-of-sample: the model is retrained without the final "
        f"{metrics.test_window_days} day(s) and scored against those held-out days."
        if metrics is not None
        else "No validation window was available, so no accuracy figure is claimed.",
    ]
    if share is not None:
        notes.append(
            f"Peak/off-peak split assumes peak-window usage stays at the historical {share * 100:.0f}% share of daily energy."
        )
    notes.extend(notes_tail)

    return DailyForecast(
        status="ok",
        method=method,
        model=model_name,
        generated_at=generated_at,
        horizon_days=horizon_days,
        history_days=observed_days,
        first_day_forecast=points[0].date if points else "",
        last_day_forecast=points[-1].date if points else "",
        metrics=metrics,
        points=points,
        predicted_energy_kwh=total,
        predicted_avg_kwh_per_day=total / horizon_days if horizon_days else 0.0,
        peak_share=share,
        predicted_peak_kwh=predicted_peak,
        predicted_offpeak_kwh=predicted_offpeak,
        confidence=confidence,
        confidence_reason=reason,
        band_note=(
            f"The shaded range is the predicted value plus or minus {BAND_SIGMA:.2f} times the validation "
            f"root-mean-square error ({sigma:.2f} kWh), which is roughly an 80% range under a normal-error assumption."
        ),
        excluded_incomplete_days=excluded_incomplete,
        last_complete_day=last_complete_day.date().isoformat(),
        warnings=warnings,
        notes=notes,
    )
