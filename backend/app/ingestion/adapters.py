"""Device adapters: the one place a source turns into a validated sample.

The simulator, CSV import and MQTT poll already share `normalize_sample`. Hardware gets the
same treatment here rather than a parallel write path. An adapter is responsible for the
things that are genuinely source-specific:

* the source label and the simulated/measured declaration,
* turning a sensor's raw fields into the common measurement shape,
* adding a flag when a figure is derived rather than measured.

Everything after `prepare()` goes through `persist_samples`, so ownership, idempotency and
quality flags behave identically for simulated and hardware data.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.app.ingestion.base import (
    ReadingSample,
    SampleRejected,
    derive_energy,
    normalize_sample,
)


@dataclass
class AdapterContext:
    """What an adapter needs to know about the device it is writing for."""

    device_id: str
    sampling_interval_seconds: int | None = None


class DeviceAdapter:
    """Base class for a source-specific reader."""

    source = "unknown"
    is_simulated: bool | None = None

    def prepare(self, payload: Any, context: AdapterContext) -> ReadingSample:
        raise NotImplementedError


class SimulatorAdapter(DeviceAdapter):
    source = "simulator"
    is_simulated = True

    def prepare(self, payload: Any, context: AdapterContext) -> ReadingSample:
        data = dict(payload or {})
        data["device_id"] = context.device_id
        data["is_simulated"] = True
        return normalize_sample(data, source=self.source)


class CsvAdapter(DeviceAdapter):
    source = "csv"

    def prepare(self, payload: Any, context: AdapterContext) -> ReadingSample:
        data = dict(payload or {})
        data["device_id"] = context.device_id
        return normalize_sample(data, source=self.source)


class GenericAdapter(DeviceAdapter):
    """A source with no special rules. Its provenance stays whatever it declared."""

    def __init__(self, source: str | None = None) -> None:
        self.source = source or "unknown"

    def prepare(self, payload: Any, context: AdapterContext) -> ReadingSample:
        data = dict(payload or {})
        data["device_id"] = context.device_id
        return normalize_sample(data, source=self.source)


class Esp32Adapter(DeviceAdapter):
    """The ESP32 / ACS712-family hardware contract.

    A current sensor measures current. Real power also needs voltage and a power factor.
    When the firmware sends voltage and current but no power, this adapter calculates an
    apparent power and marks it as an estimate with power factor assumed to be 1. When it
    sends only current, the sample is refused rather than inventing watts.
    """

    source = "esp32"

    def prepare(self, payload: Any, context: AdapterContext) -> ReadingSample:
        data = dict(payload or {})
        data["device_id"] = context.device_id
        data["source"] = self.source
        # `simulated` is the firmware's own declaration. A device that says it is a
        # simulator is labelled simulated even if its token says esp32.
        data["is_simulated"] = bool(data.pop("simulated", False))

        voltage = data.get("voltage")
        current = data.get("current")
        power = data.get("power_w")
        energy = data.get("energy_wh")
        factor = data.pop("power_factor", None)

        derived_flags: list[str] = []
        if power is None and voltage is not None and current is not None:
            try:
                apparent = float(voltage) * float(current)
                if factor is not None:
                    data["power_w"] = round(apparent * float(factor), 6)
                    derived_flags.append(
                        "real power calculated from voltage, current and the reported power factor"
                    )
                else:
                    data["power_w"] = round(apparent, 6)
                    derived_flags.append(
                        "real power estimated as voltage x current with power factor assumed to be 1"
                    )
            except (TypeError, ValueError):
                pass
        elif power is None and energy is None and current is not None:
            raise SampleRejected(
                "A current-only sample cannot provide real power. Send voltage or power_w, "
                "or send energy_wh for one interval."
            )

        if energy is None and data.get("power_w") is not None:
            data["_derive_energy_from_interval"] = True

        sample = normalize_sample(data, source=self.source)
        if data.get("_derive_energy_from_interval"):
            derive_energy(sample, context.sampling_interval_seconds)
            derived_flags.append(
                "energy estimated from power and the configured sampling interval, not measured"
            )
        sample.quality_flags.extend(derived_flags)
        return sample


ADAPTERS: dict[str, DeviceAdapter] = {
    "simulator": SimulatorAdapter(),
    "csv": CsvAdapter(),
    "esp32": Esp32Adapter(),
}


def adapter_for(source: str | None) -> DeviceAdapter:
    key = (source or "").strip().lower()
    if key in ADAPTERS:
        return ADAPTERS[key]
    return GenericAdapter(source)
