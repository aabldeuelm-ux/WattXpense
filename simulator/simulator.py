from __future__ import annotations

import random
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from backend.app.ingestion.base import ReadingSample
from simulator.profiles import (
    DeviceProfile,
    InjectedAnomaly,
    default_profiles,
    generate_readings,
    plan_injected_anomalies,
)

DEVICE_ID = "sim-main-01"


@dataclass
class SimulationPlan:
    profiles: tuple[DeviceProfile, ...]
    anomalies: list[InjectedAnomaly]
    gaps: list[str]
    seed: int
    start: datetime
    end: datetime

    def samples(self) -> list[ReadingSample]:
        collected: list[ReadingSample] = []
        for index, profile in enumerate(self.profiles):
            collected.extend(
                generate_readings(
                    profile,
                    self.start,
                    self.end,
                    seed=self.seed + index * 977,
                    anomalies=self.anomalies if profile.is_primary else (),
                    gaps=self.gaps if profile.is_primary else (),
                )
            )
        collected.sort(key=lambda item: (item.device_id, item.ts))
        return collected


def build_plan(
    *,
    days: int = 35,
    end: datetime | None = None,
    seed: int = 20260926,
    inject_anomalies: bool = True,
    inject_gap: bool = True,
    profiles: tuple[DeviceProfile, ...] | None = None,
) -> SimulationPlan:
    profiles = profiles or default_profiles()
    end = (end or datetime.now(timezone.utc)).astimezone(timezone.utc).replace(second=0, microsecond=0)
    start_day = (end - timedelta(days=days)).replace(hour=0, minute=0, second=0, microsecond=0)

    rng = random.Random(seed)
    anomalies = plan_injected_anomalies(start_day, days, rng) if inject_anomalies else []

    gaps: list[str] = []
    if inject_gap and days >= 10:
        anomaly_days = {a.day for a in anomalies}
        for offset in range(days - 3, days - 9, -1):
            candidate = (start_day + timedelta(days=offset)).date().isoformat()
            if candidate not in anomaly_days:
                gaps.append(candidate)
                break

    return SimulationPlan(
        profiles=profiles,
        anomalies=anomalies,
        gaps=gaps,
        seed=seed,
        start=start_day,
        end=end,
    )


def live_sample(
    profile: DeviceProfile,
    *,
    now: datetime | None = None,
    seed: int = 7,
) -> ReadingSample:
    rng = random.Random(seed)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    day = now.replace(hour=0, minute=0, second=0, microsecond=0)
    curve = _live_curve(profile, now, day, rng)
    minute_of_day = min(now.hour * 60 + now.minute, 1439)
    power_w = curve[minute_of_day]
    sag = min(7.0, power_w / 600.0)
    voltage = round(max(profile.nominal_voltage_v - sag + rng.gauss(0.0, 1.6), 170.0), 2)
    power_factor = rng.uniform(0.92, 0.995) if power_w > 300 else 1.0
    current = round(power_w / (voltage * power_factor), 4) if voltage > 0 else 0.0
    return ReadingSample(
        device_id=profile.device_id,
        ts=now,
        voltage=voltage,
        current=current,
        power_w=round(power_w, 2),
        energy_wh=round(power_w * profile.interval_hours, 4),
        source="simulator",
        is_simulated=True,
    )


def _live_curve(profile: DeviceProfile, now: datetime, day: datetime, rng: random.Random) -> list[float]:
    from simulator.profiles import _day_curve

    return _day_curve(day, profile, rng)
