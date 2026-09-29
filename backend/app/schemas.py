"""Request and response models.

Validation that belongs to the shape of a request lives here; validation that belongs to
whether a reading makes physical sense lives in the ingestion path, so that one bad
sample is reported per sample rather than failing a whole batch.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from backend.app.config import SlabConfigError, validate_slabs

RateType = Literal["slab", "flat", "time_of_use"]


def _require_aware(value: datetime) -> datetime:
    """A naive timestamp is rejected rather than assumed to be UTC.

    Assuming would file a reading from an offset-naive exporter under the wrong day,
    which then surfaces as a phantom spike or a phantom gap.
    """
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError("ts must include a timezone offset, for example 2026-09-26T14:00:00Z")
    return value


# ---------------------------------------------------------------------------------------
# Readings and devices
# ---------------------------------------------------------------------------------------


class ReadingIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    device_id: str = Field(min_length=1)
    ts: datetime
    voltage: float | None = None
    current: float | None = None
    power_w: float | None = None
    energy_wh: float | None = None
    source: str | None = None
    is_simulated: bool | None = None

    @field_validator("ts")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)


class ReadingsBatchIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    readings: list[ReadingIn] = Field(default_factory=list)
    source: str | None = None


class ManualReadingIn(BaseModel):
    # Manual entry is a human typing a reading, so a stray field is far more likely to be
    # a typo than an extension. Refuse it rather than quietly dropping it.
    model_config = ConfigDict(extra="forbid")

    device_id: str = Field(min_length=1)
    ts: datetime
    voltage: float | None = None
    current: float | None = None
    power_w: float | None = None
    energy_wh: float | None = None

    @field_validator("ts")
    @classmethod
    def _aware(cls, value: datetime) -> datetime:
        return _require_aware(value)

    @model_validator(mode="after")
    def _needs_a_measurement(self) -> "ManualReadingIn":
        if all(getattr(self, name) is None for name in ("voltage", "current", "power_w", "energy_wh")):
            raise ValueError("Enter at least one of power_w, energy_wh, or voltage and current")
        return self


class DeviceIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    id: str = Field(min_length=1, max_length=120)
    name: str | None = None
    location: str | None = None
    source: str | None = None
    is_simulated: bool | None = None
    is_primary: bool | None = None
    nominal_voltage_v: float | None = None
    notes: str | None = None


# ---------------------------------------------------------------------------------------
# Tariffs
# ---------------------------------------------------------------------------------------


class SlabIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    up_to_kwh: float | None = None
    rate_per_kwh: float = Field(ge=0)


class TariffIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = ""
    rate_type: RateType = "flat"
    currency: str = "INR"
    flat_rate_per_kwh: float | None = Field(default=None, ge=0)
    peak_rate_per_kwh: float | None = Field(default=None, ge=0)
    offpeak_rate_per_kwh: float | None = Field(default=None, ge=0)
    peak_start_hour: int | None = Field(default=None, ge=0, le=23)
    peak_end_hour: int | None = Field(default=None, ge=0, le=23)
    standing_charge_per_day: float = Field(default=0.0, ge=0)
    tax_percent: float = Field(default=0.0, ge=0)
    billing_start_day: int = Field(default=1, ge=1, le=28)
    slabs: list[SlabIn] | None = None
    is_active: bool = True

    @model_validator(mode="after")
    def _check_rates(self) -> "TariffIn":
        if self.rate_type == "slab":
            blocks = [block.model_dump() for block in (self.slabs or [])]
            # Same rules as the environment override, so a tariff the operator typed into
            # the UI cannot be one the app would have refused from a variable.
            validate_slabs(blocks)
        elif self.rate_type == "flat":
            if self.flat_rate_per_kwh is None:
                raise ValueError("A flat tariff needs a flat_rate_per_kwh")
        else:
            if self.peak_rate_per_kwh is None or self.offpeak_rate_per_kwh is None:
                raise ValueError("A time-of-use tariff needs both peak and off-peak rates")
            if self.peak_rate_per_kwh <= self.offpeak_rate_per_kwh:
                raise ValueError("The peak rate must be higher than the off-peak rate, or the peak is not worth avoiding")
            if self.peak_start_hour is None or self.peak_end_hour is None:
                raise ValueError("A time-of-use tariff needs a peak window")
        return self

    def slab_dicts(self) -> list[dict[str, Any]]:
        return [block.model_dump() for block in (self.slabs or [])]


# ---------------------------------------------------------------------------------------
# AI
# ---------------------------------------------------------------------------------------


class ExplainIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    device_id: str | None = None
    horizon_days: int = Field(default=7, ge=1, le=90)
