"""Prompt construction, the grounding check, and the calculated fallback.

Three rules shape this module:

1. **The model never produces a number.** It receives figures the app has already
   calculated and is asked to describe them, not to compute them.
2. **Fabricated figures are rejected.** Every number in the model's prose is checked
   against the figures it was given, and an answer containing an invented one is
   discarded rather than trimmed.
3. **The model never sees device identity.** No device id, name or location is included
   in the context, and the prompt forbids naming appliances the data does not mention.
"""

from __future__ import annotations

import json
import re
from typing import Any

SYSTEM_PROMPT = """You are writing a short plain-language summary of household electricity \
usage for the person who lives there. You will be given a block of JSON containing figures \
that another program has already calculated.

Rules you must follow:

- Use only the figures in the JSON. Never calculate, convert, round differently, or \
invent a number, a percentage, a rate or a cost. If a figure you want is not in the JSON, \
write the sentence without it.
- Never name a device, meter, appliance, room or location, not even as an example. The \
data is whole-site and does not identify which appliance used the power. Refer to "your \
home", "the metered site" or "your usage" instead.
- Do not present an average or blended rate as a fixed per-unit price. The JSON may \
include a field called rate_note; if it is present, its wording must be respected, and you \
must not call a slab tariff a flat rate.
- Causes are hypotheses, not diagnoses. Write them as things to check, never as findings.
- Keep it short: one headline, at most four sentences of explanation, at most four \
suggestions, and any caveats you think a careful reader would want.
- Return JSON only, with exactly these keys: headline (string), explanation (string), \
suggestions (array of strings), caveats (array of strings)."""

USER_INSTRUCTION = """Here are the calculated figures. Write the summary now, as JSON only."""

# Long lists are capped: a 20-item array would crowd out the figures that matter and
# makes it easier for a model to start interpolating.
MAX_BANDS = 5
MAX_SUGGESTIONS = 5
MAX_FINDINGS = 5
MAX_EVIDENCE = 3
MAX_FORECAST_DAYS = 14


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _fmt(value: Any) -> str | None:
    """Render a figure for prose, consistently with the grounding check's rounding."""
    number = _number(value)
    if number is None:
        return None
    rounded = round(number, 3)
    if rounded == int(rounded):
        return str(int(rounded))
    return f"{rounded:.3f}".rstrip("0").rstrip(".")


def build_context(
    *,
    summary: dict,
    insights: dict,
    forecast: dict,
    anomalies: dict,
    data_label: str,
) -> dict:
    """Distil the dashboard's calculated output into the small block the model sees.

    Device identity is deliberately absent: there is no per-appliance breakdown to infer
    from anyway, and a device name in a prompt is a name that can end up in the answer.
    """
    summary = summary or {}
    insights = insights or {}
    forecast = forecast or {}
    anomalies = anomalies or {}

    total_kwh = _number(insights.get("total_energy_kwh")) or _number((summary.get("energy_this_period") or {}).get("energy_kwh"))
    days = _number(insights.get("days_analysed"))
    average = _number(insights.get("average_kwh_per_day"))
    if average is None and total_kwh is not None and days:
        average = round(total_kwh / days, 3)

    bands = [
        {
            "label": band.get("label"),
            "energy_kwh": _number(band.get("energy_kwh")),
            "energy_share_percent": _number(band.get("energy_share_percent")),
        }
        for band in (insights.get("load_bands") or [])[:MAX_BANDS]
    ]

    suggestions = [
        {
            "title": item.get("title"),
            "potential_saving_kwh_per_month": _number(item.get("savings_kwh_per_month")),
            "potential_saving_cost_per_month": _number(item.get("savings_cost_per_month")),
            "evidence": list(item.get("evidence") or [])[:MAX_EVIDENCE],
        }
        for item in (insights.get("insights") or [])[:MAX_SUGGESTIONS]
    ]

    points = forecast.get("points") or []
    forecast_days = [
        {"date": point.get("date"), "energy_kwh": _number(point.get("energy_kwh")), "kind": "forecast"}
        for point in points
        if (point.get("kind") or "forecast") == "forecast"
    ][:MAX_FORECAST_DAYS]

    findings = [
        {
            "title": item.get("title"),
            "kind": item.get("kind"),
            "severity": item.get("severity"),
            "evidence": list(item.get("evidence") or [])[:MAX_EVIDENCE],
        }
        for item in (anomalies.get("findings") or [])[:MAX_FINDINGS]
    ]

    bill = summary.get("projected_bill") or {}
    bill_energy = bill.get("energy") or {}
    projected_bill = {
        "total_cost": _number(bill.get("total")),
        "energy_kwh": _number(bill_energy.get("total_kwh")),
        "energy_charge": _number(bill.get("energy_charge")),
        "fixed_charge": _number(bill.get("standing_charge")),
        "duty": _number(bill.get("tax")),
        "duty_label": bill.get("duty_label"),
        "basis": bill.get("projection_basis"),
        "is_estimate": bool(bill.get("is_estimate", True)),
    }

    context = {
        "scope": "the metered site",
        "data_label": data_label,
        "currency": (summary.get("tariff") or {}).get("currency"),
        "usage": {
            "days_analysed": days,
            "total_energy_kwh": total_kwh,
            "average_kwh_per_day": average,
        },
        "load_bands": bands,
        "forecast": {
            "status": forecast.get("status"),
            "horizon_days": _number(forecast.get("horizon_days")),
            "predicted_avg_kwh_per_day": _number(forecast.get("predicted_avg_kwh_per_day")),
            "confidence": forecast.get("confidence"),
            "next_days": forecast_days,
        },
        "anomalies": {
            "status": anomalies.get("status"),
            "count": len(anomalies.get("findings") or []),
            "findings": findings,
        },
        "calculated_suggestions": suggestions,
        "projected_bill": projected_bill,
        "tariff": _tariff_context(summary),
    }
    return context


def _tariff_context(summary: dict) -> dict:
    tariff = summary.get("tariff") or {}
    rate_type = tariff.get("rate_type")
    context: dict[str, Any] = {
        "name": tariff.get("name"),
        "rate_type": rate_type,
        "currency": tariff.get("currency"),
        "effective_rate_per_kwh": None,
        "rate_note": "",
    }

    if rate_type == "slab":
        # A slab tariff has no single rate, so quoting one as a flat price would be
        # wrong. The period cost divided by the period energy is the average actually
        # being paid, which is a real figure rather than an absent one read as zero.
        period = summary.get("energy_this_period") or {}
        energy = _number(period.get("energy_kwh"))
        cost = _number(period.get("cost"))
        if energy and cost is not None:
            rate = round(cost / energy, 4)
            context["effective_rate_per_kwh"] = rate
            context["rate_note"] = (
                f"{rate} {tariff.get('currency') or ''}/kWh is an average across the slabs of this bill, "
                "not a fixed price: each unit is charged at the rate of the slab it falls in, so the "
                "average changes as consumption grows."
            ).replace(" /kWh", "/kWh")
            return context
        slabs = tariff.get("slabs") or []
        if slabs:
            top = _number(slabs[-1].get("rate_per_kwh"))
            context["effective_rate_per_kwh"] = top
            context["rate_note"] = (
                f"{top} {tariff.get('currency') or ''}/kWh is the highest rate in this slab tariff, not a "
                "fixed price: lower units are charged at lower slab rates."
            ).replace(" /kWh", "/kWh")
        return context

    for key in ("flat_rate_per_kwh", "peak_rate_per_kwh"):
        value = _number(tariff.get(key))
        if value:
            context["effective_rate_per_kwh"] = value
            return context
    return context


def build_user_prompt(context: dict) -> str:
    return (
        f"{USER_INSTRUCTION}\n\n<data>\n"
        f"{json.dumps(context, indent=2, default=str, ensure_ascii=False)}\n"
        "</data>"
    )


# ---------------------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------------------

# A number, with optional thousands separators, followed by an optional unit. Only
# numbers carrying a unit or written in full figures are treated as measurements, so
# ordinary counting words are left alone.
_MEASUREMENT = re.compile(
    r"(?<![\w.,])(\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s*"
    r"(kWh|Wh|kW|VA|kVA|W|V|A|%|USD|INR|EUR|GBP|CAD|AUD|units?|hours?|hrs?|days?|minutes?|mins?)?",
    re.IGNORECASE,
)

_CURRENCY_SYMBOL = re.compile(r"[$€£₹]")


def _collect_numbers(value: Any, out: set[float]) -> None:
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        out.add(round(float(value), 3))
        return
    if isinstance(value, dict):
        for item in value.values():
            _collect_numbers(item, out)
    elif isinstance(value, (list, tuple)):
        for item in value:
            _collect_numbers(item, out)


def _is_list_marker(text: str, start: int, end: int) -> bool:
    """`1. Shift laundry` is an ordered list, not a measurement of one thing."""
    line_before = text.rfind("\n", 0, start) + 1
    if text[line_before:start].strip():
        return False
    return text[end:].lstrip().startswith(".")


def find_ungrounded_measurements(text: str, context: dict) -> list[str]:
    """Every figure in `text` that is not one of the figures we supplied.

    An empty list means the answer is safe to show. A non-empty one means the model
    invented something, and the whole answer is thrown away rather than partly trusted.
    """
    if not text:
        return []
    allowed: set[float] = set()
    _collect_numbers(context, allowed)

    ungrounded: list[str] = []
    for match in _MEASUREMENT.finditer(text):
        if _is_list_marker(text, match.start(), match.end()):
            continue
        raw = match.group(1).replace(",", "")
        try:
            value = round(float(raw), 3)
        except ValueError:
            continue
        if value in allowed:
            continue
        fragment = match.group(0).strip()
        prefix = text[max(match.start() - 1, 0) : match.start()]
        if _CURRENCY_SYMBOL.search(prefix):
            fragment = f"{prefix}{fragment}"
        ungrounded.append(fragment)
    return ungrounded


def fallback_narrative(context: dict) -> dict:
    """The explanation the app writes itself, used whenever no model is available.

    It says only what the calculations support, and every figure in it is taken straight
    from the context, so it is grounded by construction.
    """
    context = context or {}
    usage = context.get("usage") or {}
    forecast = context.get("forecast") or {}

    days = _fmt(usage.get("days_analysed"))
    total = _fmt(usage.get("total_energy_kwh"))
    average = _fmt(usage.get("average_kwh_per_day"))
    horizon = _fmt(forecast.get("horizon_days"))
    predicted = _fmt(forecast.get("predicted_avg_kwh_per_day"))

    sentences: list[str] = []
    if total and days:
        sentence = f"Over the last {days} days the metered site recorded {total} kWh"
        if average:
            sentence += f", about {average} kWh a day"
        sentences.append(sentence + ".")
    elif total:
        sentences.append(f"The metered site has recorded {total} kWh.")
    else:
        sentences.append("There is not enough recorded data to summarise usage yet.")

    if predicted and horizon:
        sentences.append(f"The forecast puts the next {horizon} days at about {predicted} kWh a day.")

    bills = [
        item
        for item in (context.get("calculated_suggestions") or [])
        if isinstance(item, dict) and item.get("title")
    ]
    if bills:
        sentences.append("The figures below are the ones the app calculated on its own.")

    suggestions: list[str] = [str(item["title"]) for item in bills[:4]]
    if not suggestions:
        suggestions = [
            "Keep recording readings so the app has something to compare against.",
            "Check the tariff panel against a real bill, since the default is an example.",
        ]

    return {
        "headline": (
            f"{total} kWh recorded over {days} days" if total and days else "Usage summary from your own readings"
        ),
        "explanation": " ".join(sentences),
        "suggestions": suggestions,
        "caveats": [
            "This text was assembled from WattXpense's own calculations, not by a language model.",
            "Any suggestion above is a thing to check, not a finding about your home.",
        ],
    }
