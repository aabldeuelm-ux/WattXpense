"""The shared ingestion contract.

Every source, whatever it is, produces the same shape and is validated in this one place,
so a bad MQTT message is treated exactly the same way as a bad CSV row.

Two deliberate rules live here:

* A naive timestamp is rejected rather than assumed to be UTC.
* Physically implausible-but-plausible-looking readings (0 V, voltage and current that
  disagree with power) are flagged and kept, not dropped. Dropping them would turn a
  sensor fault into an invisible data gap, which reads as "the house used no power".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from backend.app.config import get_settings
from backend.app.timestamps import parse_timestamp, utc_now

# Sources that cannot be trusted to declare themselves: a source name in this set is
# assumed to be the simulator.
SIMULATED_SOURCES = {"simulator", "simulate", "demo"}

# Sources that only ever report a real meter. A name in this set is affirmative proof of
# hardware, so provenance is recorded as metered even if the sample forgot to say so. This
# is deliberately narrow: an unrecognised source stays unknown rather than being trusted.
HARDWARE_SOURCES = {"esp32", "esp8266", "pzem", "arduino", "hardware", "meter"}

# Reporting cadence assumed for a device that has not reported enough to show its own.
DEFAULT_INTERVAL_SECONDS = 3600


class SampleRejected(ValueError):
    """A sample that cannot be stored at all. One of these must not cost us the batch."""


@dataclass
class ReadingSample:
    device_id: str
    ts: datetime
    voltage: float | None = None
    current: float | None = None
    power_w: float | None = None
    energy_wh: float | None = None
    source: str | None = None
    is_simulated: bool | None = None
    quality_flags: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        from backend.app.timestamps import iso

        return {
            "device_id": self.device_id,
            "ts": iso(self.ts),
            "voltage": self.voltage,
            "current": self.current,
            "power_w": self.power_w,
            "energy_wh": self.energy_wh,
            "source": self.source,
            "is_simulated": self.is_simulated,
        }


def flag_list(flags: list[str]) -> str | None:
    return "|".join(flags) if flags else None


def simulated_for(source: str | None, declared: bool | None = None) -> bool | None:
    """Resolve the tri-state `is_simulated`.

    A source that declares itself wins. A source we know to be the simulator is marked
    simulated even if it says otherwise. Anything else is left unset, because guessing
    "not simulated" would be a claim we cannot support.
    """
    if declared is not None:
        return bool(declared)
    if source and source.strip().lower() in SIMULATED_SOURCES:
        return True
    if source and source.strip().lower() in HARDWARE_SOURCES:
        return False
    return None


def _number(value: Any, field_name: str) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise SampleRejected(f"{field_name} must be a number, got a boolean")
    try:
        return float(value)
    except (TypeError, ValueError):
        raise SampleRejected(f"{field_name} must be a number, got {value!r}") from None


def derive_missing(sample: ReadingSample) -> ReadingSample:
    """Fill in whichever of voltage, current and power the source did not supply.

    Any one of the three implies the other two, so a source only has to send what it
    actually measures. A circular derivation is refused rather than guessed at.
    """
    if sample.power_w is None:
        if sample.voltage and sample.current:
            sample.power_w = round(sample.voltage * sample.current, 6)
    if sample.current is None and sample.voltage and sample.power_w is not None:
        sample.current = round(sample.power_w / sample.voltage, 6)
    if sample.voltage is None and sample.current and sample.power_w is not None:
        sample.voltage = round(sample.power_w / sample.current, 6)
    return sample


def derive_energy(sample: ReadingSample, interval_seconds: int | None = None) -> ReadingSample:
    """Fill in energy for a source that reported power over a known interval.

    The interval comes from the device's own observed reporting cadence when we have it,
    so a manual entry is costed over the same window the device actually reports in.
    A cumulative meter reading is never reconstructed here: subtracting one is a guess
    about when the meter was read, and that guess would be invisible in the totals.
    """
    if sample.energy_wh is None and sample.power_w is not None:
        seconds = interval_seconds or DEFAULT_INTERVAL_SECONDS
        if seconds > 0:
            sample.energy_wh = round(sample.power_w * seconds / 3600.0, 4)
    return sample


def quality_flags(sample: ReadingSample, *, nominal_voltage_v: float | None = None) -> list[str]:
    """Everything suspicious about a reading, as short human-readable notes.

    Nothing here is a reason to discard the sample.
    """
    settings = get_settings()
    flags: list[str] = []

    if sample.voltage is not None and sample.voltage <= 0:
        flags.append("voltage reported as 0 V, which is implausible; the sample is kept so the gap is visible")
    elif sample.voltage is not None:
        nominal = nominal_voltage_v if nominal_voltage_v is not None else settings.nominal_voltage_v
        if nominal and abs(sample.voltage - nominal) / nominal * 100.0 > settings.voltage_tolerance_percent:
            flags.append(
                f"voltage {sample.voltage:.1f} V is more than {settings.voltage_tolerance_percent:.0f}% "
                f"from the nominal {nominal:.0f} V"
            )

    if sample.voltage and sample.current is not None and sample.power_w is not None:
        apparent = sample.voltage * sample.current
        if apparent > 0:
            factor = sample.power_w / apparent
            if factor < 0.1 or factor > 1.1:
                flags.append(
                    f"voltage x current ({apparent:.0f} VA) disagrees with power_w "
                    f"({sample.power_w:.0f} W), so the implied power factor is {factor:.2f}"
                )

    now = utc_now()
    if sample.ts > now + timedelta(minutes=1):
        flags.append("timestamp_in_future")

    return flags


def normalize_sample(payload: Any, source: str | None = None) -> ReadingSample:
    """Turn a raw payload into a validated sample, or raise `SampleRejected`.

    The rejection messages name the offending field so the caller can act on them.
    """
    if isinstance(payload, ReadingSample):
        sample = payload
    else:
        data = dict(payload or {})
        device_id = str(data.get("device_id") or "").strip()
        if not device_id:
            raise SampleRejected("device_id is required")

        ts = parse_timestamp(data.get("ts"))
        if ts is None:
            raise SampleRejected("ts must be a timezone-aware timestamp, for example 2026-09-26T14:00:00Z")

        declared_source = data.get("source") or source
        declared_simulated = data.get("is_simulated")
        if declared_simulated is not None and not isinstance(declared_simulated, bool):
            declared_simulated = bool(declared_simulated)

        sample = ReadingSample(
            device_id=device_id,
            ts=ts,
            voltage=_number(data.get("voltage"), "voltage"),
            current=_number(data.get("current"), "current"),
            power_w=_number(data.get("power_w"), "power_w"),
            energy_wh=_number(data.get("energy_wh"), "energy_wh"),
            source=(str(declared_source) if declared_source is not None else None),
            is_simulated=simulated_for(declared_source, declared_simulated),
        )

    # Impossibilities are rejected outright: they cannot be a real measurement, and
    # storing them would corrupt every aggregate downstream. Every offending field is
    # named at once, so a caller fixing a bad payload does not discover the rest by
    # resubmitting it one field at a time.
    negative = [
        f"{name}={getattr(sample, name):g}"
        for name in ("voltage", "current", "power_w", "energy_wh")
        if getattr(sample, name) is not None and getattr(sample, name) < 0
    ]
    if negative:
        raise SampleRejected(
            f"{', '.join(negative)} cannot be a measurement. voltage, current, power_w and "
            "energy_wh must not be negative."
        )

    derive_missing(sample)

    if sample.power_w is None and sample.energy_wh is None:
        raise SampleRejected("a reading needs at least one of power_w or energy_wh")

    sample.quality_flags = quality_flags(sample)
    return sample
