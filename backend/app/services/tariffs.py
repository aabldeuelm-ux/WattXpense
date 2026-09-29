"""Tariff persistence and effective rates.

A slab tariff has no single rate, so this module is careful to say *which* rate any figure
is: the top of the tariff, or the average actually being paid at the current consumption.
Presenting one as the other is how a bill ends up quoted at a price that does not exist.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.models import Tariff

# An Indian bill calls it electricity duty. Anywhere else the honest name is generic.
DUTY_LABELS = {"INR": "Electricity duty"}


@dataclass
class TariffConfig:
    name: str
    rate_type: str
    currency: str
    standing_charge_per_day: float = 0.0
    tax_percent: float = 0.0
    billing_start_day: int = 1
    flat_rate_per_kwh: float | None = None
    peak_rate_per_kwh: float | None = None
    offpeak_rate_per_kwh: float | None = None
    peak_start_hour: int | None = None
    peak_end_hour: int | None = None
    slabs: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def duty_label(self) -> str:
        return DUTY_LABELS.get(self.currency.upper(), "Tax / levy")

    @property
    def is_slab(self) -> bool:
        return self.rate_type == "slab" and bool(self.slabs)

    @property
    def is_time_of_use(self) -> bool:
        return self.rate_type == "time_of_use" and self.peak_rate_per_kwh is not None


def duty_label(currency: str) -> str:
    return DUTY_LABELS.get((currency or "").upper(), "Tax / levy")


def to_config(tariff: Tariff) -> TariffConfig:
    """Turn a stored tariff into the plain value object the arithmetic works on."""
    return TariffConfig(
        name=tariff.name,
        rate_type=tariff.rate_type,
        currency=tariff.currency,
        standing_charge_per_day=float(tariff.standing_charge_per_day or 0.0),
        tax_percent=float(tariff.tax_percent or 0.0),
        billing_start_day=int(tariff.billing_start_day or 1),
        flat_rate_per_kwh=tariff.flat_rate_per_kwh,
        peak_rate_per_kwh=tariff.peak_rate_per_kwh,
        offpeak_rate_per_kwh=tariff.offpeak_rate_per_kwh,
        peak_start_hour=tariff.peak_start_hour,
        peak_end_hour=tariff.peak_end_hour,
        slabs=tuple(tariff.slab_list()),
    )


def default_config() -> TariffConfig:
    """The example slab tariff a fresh install comes up with."""
    settings = get_settings()
    if settings.default_rate_type == "slab":
        return TariffConfig(
            name=settings.default_tariff_name,
            rate_type="slab",
            currency=settings.currency,
            standing_charge_per_day=settings.default_standing_charge_per_day,
            tax_percent=settings.default_tax_percent,
            billing_start_day=settings.default_billing_start_day,
            peak_start_hour=settings.default_peak_start_hour,
            peak_end_hour=settings.default_peak_end_hour,
            slabs=tuple(settings.default_slabs),
        )
    return TariffConfig(
        name=settings.default_tariff_name,
        rate_type=settings.default_rate_type,
        currency=settings.currency,
        standing_charge_per_day=settings.default_standing_charge_per_day,
        tax_percent=settings.default_tax_percent,
        billing_start_day=settings.default_billing_start_day,
        flat_rate_per_kwh=settings.default_flat_rate_per_kwh or None,
        peak_rate_per_kwh=settings.default_flat_rate_per_kwh or None,
        offpeak_rate_per_kwh=settings.default_flat_rate_per_kwh or None,
        peak_start_hour=settings.default_peak_start_hour,
        peak_end_hour=settings.default_peak_end_hour,
    )


def get_active(db: Session) -> Tariff:
    """The tariff in force, creating the default example on first run.

    Creating it lazily rather than at migration time means a database that has had its
    tariffs cleared still answers the billing screen.
    """
    tariff = db.execute(
        select(Tariff).where(Tariff.is_active.is_(True)).order_by(Tariff.id)
    ).scalars().first()
    if tariff is not None:
        return tariff

    config = default_config()
    tariff = Tariff(
        name=config.name,
        rate_type=config.rate_type,
        currency=config.currency,
        flat_rate_per_kwh=config.flat_rate_per_kwh,
        peak_rate_per_kwh=config.peak_rate_per_kwh,
        offpeak_rate_per_kwh=config.offpeak_rate_per_kwh,
        peak_start_hour=config.peak_start_hour,
        peak_end_hour=config.peak_end_hour,
        standing_charge_per_day=config.standing_charge_per_day,
        tax_percent=config.tax_percent,
        billing_start_day=config.billing_start_day,
        slabs=json.dumps(list(config.slabs)) if config.slabs else None,
        is_active=True,
    )
    db.add(tariff)
    db.commit()
    db.refresh(tariff)
    return tariff


def get_config(db: Session) -> TariffConfig:
    return to_config(get_active(db))


def list_tariffs(db: Session) -> list[Tariff]:
    return list(db.execute(select(Tariff).order_by(Tariff.id.desc())).scalars())


def as_dict(tariff: Tariff) -> dict:
    return {
        "id": tariff.id,
        "name": tariff.name,
        "rate_type": tariff.rate_type,
        "currency": tariff.currency,
        "flat_rate_per_kwh": tariff.flat_rate_per_kwh,
        "peak_rate_per_kwh": tariff.peak_rate_per_kwh,
        "offpeak_rate_per_kwh": tariff.offpeak_rate_per_kwh,
        "peak_start_hour": tariff.peak_start_hour,
        "peak_end_hour": tariff.peak_end_hour,
        "standing_charge_per_day": tariff.standing_charge_per_day,
        "tax_percent": tariff.tax_percent,
        "billing_start_day": tariff.billing_start_day,
        "slabs": tariff.slab_list(),
        "is_active": bool(tariff.is_active),
        "duty_label": duty_label(tariff.currency),
        "is_example": "example" in (tariff.name or "").lower(),
        "created_at": tariff.created_at,
    }


# ---------------------------------------------------------------------------------------
# Effective rate
# ---------------------------------------------------------------------------------------


def slab_cost(config: TariffConfig, kwh: float) -> float:
    """Price energy block by block. Never with a blended rate."""
    total = 0.0
    remaining = max(float(kwh), 0.0)
    lower = 0.0
    for block in config.slabs:
        bound = block.get("up_to_kwh")
        rate = float(block.get("rate_per_kwh") or 0.0)
        if bound is None:
            units = remaining
        else:
            units = max(min(remaining, float(bound) - lower), 0.0)
        total += units * rate
        remaining -= units
        if bound is None:
            break
        lower = float(bound)
        if remaining <= 0:
            break
    return total


def slab_marginal_rate(config: TariffConfig, cumulative_kwh: float) -> float:
    """The rate the next unit is charged at, given what has already been consumed.

    A slab bill depends on the whole period, so today's units are charged at whatever
    rate the month's running total reaches by the end of today, not at an average.
    """
    position = max(float(cumulative_kwh), 0.0)
    for block in config.slabs:
        bound = block.get("up_to_kwh")
        if bound is None:
            return float(block.get("rate_per_kwh") or 0.0)
        if position < float(bound):
            return float(block.get("rate_per_kwh") or 0.0)
    return float(config.slabs[-1].get("rate_per_kwh") or 0.0) if config.slabs else 0.0


def effective_rate(config: TariffConfig, consumption_kwh: float | None = None) -> float:
    """The rate to quote, with the consumption behind it when one is known."""
    if config.is_slab:
        if consumption_kwh and consumption_kwh > 0:
            return round(slab_cost(config, consumption_kwh) / float(consumption_kwh), 6)
        return round(float(config.slabs[-1].get("rate_per_kwh") or 0.0), 6)
    if config.is_time_of_use:
        return float(config.peak_rate_per_kwh or 0.0)
    return float(config.flat_rate_per_kwh or 0.0)


def rate_basis(config: TariffConfig, consumption_kwh: float | None = None) -> str:
    """Name the basis of `effective_rate`, so a blended average is never read as a price."""
    if config.is_slab:
        if consumption_kwh and consumption_kwh > 0:
            return "blended_across_slabs"
        return "highest_slab"
    if config.is_time_of_use:
        return "time_of_use_peak"
    return "flat_rate"


def cost_context(config: TariffConfig, consumption_kwh: float | None = None):
    """The `energy_ai` cost context, which is where the rate wording lives."""
    from energy_ai.insights import CostContext

    return CostContext(
        currency=config.currency,
        effective_rate_per_kwh=effective_rate(config, consumption_kwh),
        peak_rate_per_kwh=config.peak_rate_per_kwh,
        offpeak_rate_per_kwh=config.offpeak_rate_per_kwh,
        rate_basis=rate_basis(config, consumption_kwh),
    )
