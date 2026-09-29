"""Bill arithmetic for slab, flat and time-of-use tariffs.

A slab bill is priced block by block and never with a blended rate, because a blended
rate would produce a number that is not on any bill. Every line carries the formula that
produced it, so a user can check the working against their own invoice.
"""

from __future__ import annotations

import calendar
from datetime import date, timedelta

from backend.app.services.tariffs import TariffConfig, duty_label, slab_cost, slab_marginal_rate

# A day needs this much of itself recorded before it can be projected from its own
# readings. A day with a gap is excluded, not scaled up.
METERED_DAY_COVERAGE = 0.9

KIND_METERED = "metered"
KIND_STANDING = "standing"
KIND_TAX = "tax"


def period_bounds(today: date, billing_start_day: int) -> tuple[date, date]:
    """The billing period containing `today`, as (start, end inclusive).

    The fixed charge is billed for every day in the period including days still to
    come, which is how fixed charges actually work.
    """
    start_day = max(1, min(int(billing_start_day or 1), 28))
    if today.day >= start_day:
        start = today.replace(day=start_day)
    else:
        first_of_this_month = today.replace(day=1)
        previous_month_end = first_of_this_month - timedelta(days=1)
        start = previous_month_end.replace(day=start_day)

    next_start = (start.replace(day=28) + timedelta(days=4)).replace(day=start_day)
    return start, next_start - timedelta(days=1)


def days_elapsed(start: date, today: date) -> int:
    return max((today - start).days + 1, 1)


def _line(label: str, kind: str, quantity: float, unit: str, rate: float, currency: str) -> dict:
    amount = round(quantity * rate, 2)
    return {
        "label": label,
        "kind": kind,
        "quantity": round(quantity, 4),
        "unit": unit,
        "rate_per_unit": round(rate, 6),
        "amount": amount,
        "formula": f"{round(quantity, 4)} {unit} x {round(rate, 6)}/{currency} = {amount}",
    }


def _num(value, places: int = 2) -> str:
    if value is None:
        return "open ended"
    rounded = round(float(value), places)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.{places}f}".rstrip("0").rstrip(".")


def slab_lines(config: TariffConfig, kwh: float) -> tuple[list[dict], list[dict]]:
    """One line per block that actually carries units, plus a per-block breakdown."""
    lines: list[dict] = []
    breakdown: list[dict] = []
    remaining = max(float(kwh), 0.0)
    lower = 0.0

    for index, block in enumerate(config.slabs):
        bound = block.get("up_to_kwh")
        rate = float(block.get("rate_per_kwh") or 0.0)
        if bound is None:
            units = remaining
        else:
            units = max(min(remaining, float(bound) - lower), 0.0)

        if units > 0:
            if index == 0:
                label = f"Energy - First {_num(bound)} units @ {_num(rate)}/{config.currency}"
            elif bound is None:
                label = f"Energy - Above {_num(lower)} units @ {_num(rate)}/{config.currency}"
            else:
                label = f"Energy - Units {_num(lower + 1)}-{_num(bound)} @ {_num(rate)}/{config.currency}"
            lines.append(_line(label, KIND_METERED, units, "kWh", rate, config.currency))
            breakdown.append(
                {
                    "from_kwh": round(lower, 3),
                    "to_kwh": round(float(bound), 3) if bound is not None else None,
                    "units": round(units, 3),
                    "rate_per_kwh": rate,
                    "amount": round(units * rate, 2),
                    "open_ended": bound is None,
                }
            )

        remaining -= units
        if bound is None:
            break
        lower = float(bound)
        if remaining <= 0:
            break

    return lines, breakdown


def tax_base(config: TariffConfig, energy_charge: float, standing_charge: float) -> tuple[float, str]:
    """What the levy is charged on.

    Electricity duty is applied to the energy charge only and not to the fixed charge,
    which is the usual Indian treatment. Some DISCOMs include the fixed charge in the
    duty base, so the choice is explicit rather than hidden in a constant.
    """
    if config.currency.upper() == "INR":
        return energy_charge, "applied to the energy charge only, not to the fixed charge"
    return energy_charge + standing_charge, "applied to the energy charge and the fixed charge"


def estimate_bill(
    config: TariffConfig,
    *,
    energy_kwh: float,
    days: int,
    peak_kwh: float | None = None,
    offpeak_kwh: float | None = None,
    is_estimate: bool = True,
    notes: list[str] | None = None,
) -> dict:
    """Price a quantity of energy over a number of days, itemised."""
    energy_kwh = max(float(energy_kwh or 0.0), 0.0)
    days = max(int(days or 0), 0)
    notes = list(notes or [])
    lines: list[dict] = []
    breakdown: list[dict] = []

    if config.is_slab:
        energy_charge = round(slab_cost(config, energy_kwh), 2)
        lines, breakdown = slab_lines(config, energy_kwh)
        if energy_kwh > 0:
            notes.append(
                "Each unit is charged at the rate of the slab it falls in, so the per-unit "
                "average rises as consumption grows."
            )
    elif config.is_time_of_use:
        if peak_kwh is None and offpeak_kwh is None:
            peak_kwh, offpeak_kwh = 0.0, energy_kwh
            notes.append(
                "No peak share was supplied, so every unit is priced as off-peak. The dashboard's own "
                "bill uses the measured peak share instead."
            )
        peak_kwh = max(float(peak_kwh or 0.0), 0.0)
        offpeak_kwh = max(float(offpeak_kwh or 0.0), 0.0)
        peak_charge = round(peak_kwh * float(config.peak_rate_per_kwh or 0.0), 2)
        offpeak_charge = round(offpeak_kwh * float(config.offpeak_rate_per_kwh or 0.0), 2)
        energy_charge = round(peak_charge + offpeak_charge, 2)
        if peak_kwh > 0:
            lines.append(_line("Peak energy", KIND_METERED, peak_kwh, "kWh", float(config.peak_rate_per_kwh or 0.0), config.currency))
        if offpeak_kwh > 0:
            lines.append(
                _line("Off-peak energy", KIND_METERED, offpeak_kwh, "kWh", float(config.offpeak_rate_per_kwh or 0.0), config.currency)
            )
    else:
        rate = float(config.flat_rate_per_kwh or 0.0)
        energy_charge = round(energy_kwh * rate, 2)
        if energy_kwh > 0:
            lines.append(_line(f"Energy @ {_num(rate)}/{config.currency}", KIND_METERED, energy_kwh, "kWh", rate, config.currency))

    standing_charge = round(config.standing_charge_per_day * days, 2)
    if standing_charge > 0:
        lines.append(
            _line("Fixed charge", KIND_STANDING, days, "days", config.standing_charge_per_day, config.currency)
        )

    subtotal = round(energy_charge + standing_charge, 2)
    base, base_note = tax_base(config, energy_charge, standing_charge)
    tax = round(base * config.tax_percent / 100.0, 2)
    label = duty_label(config.currency)
    if tax > 0:
        lines.append(
            {
                "label": f"{label} ({_num(config.tax_percent)}%)",
                "kind": KIND_TAX,
                "quantity": round(base, 2),
                "unit": config.currency,
                "rate_per_unit": round(config.tax_percent, 4),
                "amount": tax,
                "formula": f"{round(base, 2)} x {_num(config.tax_percent)}% = {tax}",
            }
        )

    total = round(subtotal + tax, 2)
    if tax > 0:
        notes.append(f"{label} {base_note}.")

    return {
        "currency": config.currency,
        "tariff_name": config.name,
        "rate_type": config.rate_type,
        "energy_charge": energy_charge,
        "standing_charge": standing_charge,
        "subtotal": subtotal,
        "tax": tax,
        "tax_percent": config.tax_percent,
        "tax_base": round(base, 2),
        "duty_label": label,
        "total": total,
        "is_estimate": is_estimate,
        "lines": lines,
        "notes": notes,
        "energy": {
            "total_kwh": round(energy_kwh, 3),
            "peak_kwh": round(peak_kwh, 3) if peak_kwh is not None else None,
            "offpeak_kwh": round(offpeak_kwh, 3) if offpeak_kwh is not None else None,
            "slabs": breakdown,
        },
    }


def marginal_day_cost(config: TariffConfig, cumulative_kwh: float, day_kwh: float) -> dict:
    """Today's contribution to a slab bill, priced at the rate today's units fall in.

    A slab bill depends on the whole period, so one day's units are not billed on their
    own. This says plainly that the figure is the day's units at the marginal rate, and
    excludes the period's fixed charge and duty, which are not today's to pay.
    """
    if not config.is_slab:
        rate = float(config.flat_rate_per_kwh or 0.0) if not config.is_time_of_use else float(config.offpeak_rate_per_kwh or 0.0)
        return {
            "cost": round(day_kwh * rate, 2),
            "cost_basis": "flat_rate" if not config.is_time_of_use else "offpeak_rate",
            "rate_per_kwh": rate,
            "notes": [],
        }

    rate = slab_marginal_rate(config, cumulative_kwh)
    return {
        "cost": round(day_kwh * rate, 2),
        "cost_basis": "marginal_slab_rate",
        "rate_per_kwh": rate,
        "notes": [
            "Today's units are charged at the slab rate they fall in once the month's running "
            "total is counted, not at an average.",
            "This is today's energy contribution only: the period's fixed charge and duty are "
            "billed across the whole period and are not included.",
        ],
    }


def month_length(year: int, month: int) -> int:
    return calendar.monthrange(year, month)[1]
