"""Environment-driven settings.

Every setting has a working default, so a fresh checkout boots without a `.env`. The India
defaults matter here: WattXpense is set up for a household on a domestic Indian slab
tariff, and getting them wrong would quietly price every bill with the wrong shape.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from energy_ai.llm import LLMSettings

# A worked example of the *shape* of an Indian domestic slab tariff. It is not any
# particular DISCOM's published rates and must not be presented as one.
DEFAULT_INDIA_DOMESTIC_SLABS: tuple[dict[str, Any], ...] = (
    {"up_to_kwh": 100, "rate_per_kwh": 2.5},
    {"up_to_kwh": 300, "rate_per_kwh": 4.5},
    {"up_to_kwh": 500, "rate_per_kwh": 6.5},
    {"up_to_kwh": None, "rate_per_kwh": 8.0},
)

DEFAULT_TARIFF_NAME = "Generic India domestic (example slab tariff)"


class SlabConfigError(ValueError):
    """A slab structure that would leave consumption unpriced or price it nonsensically."""


def validate_slabs(slabs: Any) -> tuple[dict[str, Any], ...]:
    """Check a slab structure against the rules a slab tariff has to obey.

    Kept here rather than in the schema so that the environment override and the REST
    payload are held to exactly the same standard. A bad `WATTXPENSE_DEFAULT_SLABS`
    would otherwise install a default tariff the API itself later refuses.

    The rules: at least one block; the top block must be open ended so units above the
    highest boundary are still billed rather than silently free; bounds must strictly
    increase; and rates must not decrease, because a cheaper higher block contradicts
    the whole point of a progressive tariff.
    """
    if not isinstance(slabs, (list, tuple)) or not slabs:
        raise SlabConfigError("A slab tariff needs at least one block.")

    blocks: list[dict[str, Any]] = []
    for index, raw in enumerate(slabs):
        if not isinstance(raw, dict):
            raise SlabConfigError(f"Block {index + 1} is not an object.")
        if "rate_per_kwh" not in raw:
            raise SlabConfigError(f"Block {index + 1} has no rate_per_kwh.")
        rate = raw["rate_per_kwh"]
        if not isinstance(rate, (int, float)) or isinstance(rate, bool) or rate < 0:
            raise SlabConfigError(f"Block {index + 1} has a negative rate_per_kwh.")
        bound = raw.get("up_to_kwh")
        if bound is not None and (not isinstance(bound, (int, float)) or isinstance(bound, bool)):
            raise SlabConfigError(f"Block {index + 1} has a non-numeric bound.")
        if bound is not None and bound <= 0:
            raise SlabConfigError(f"Block {index + 1} has a non-positive bound.")
        blocks.append({"up_to_kwh": bound, "rate_per_kwh": float(rate)})

    last = len(blocks) - 1
    if blocks[last]["up_to_kwh"] is not None:
        raise SlabConfigError("The top slab must be open ended (up_to_kwh: null) so nothing is left unpriced.")
    for index, block in enumerate(blocks[:-1]):
        if block["up_to_kwh"] is None:
            raise SlabConfigError("Only the top slab may be open ended.")

    previous_bound: float | None = None
    previous_rate: float | None = None
    for index, block in enumerate(blocks):
        bound = block["up_to_kwh"]
        if bound is not None:
            if previous_bound is not None and bound <= previous_bound:
                raise SlabConfigError("Slab bounds must strictly increase.")
            previous_bound = float(bound)
        rate = float(block["rate_per_kwh"])
        if previous_rate is not None and rate < previous_rate:
            raise SlabConfigError("Slab rates must not decrease as consumption rises.")
        previous_rate = rate
        blocks[index]["rate_per_kwh"] = rate
    return tuple(blocks)


def _env_slabs(name: str, default: tuple[dict[str, Any], ...]) -> tuple[dict[str, Any], ...]:
    """Read a slab structure from the environment, refusing anything the API would reject.

    A malformed override falls back to the built-in example rather than installing a
    broken tariff the operator never typed into the UI.
    """
    raw = os.environ.get(name)
    if not raw or not raw.strip():
        return default
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return default
    try:
        return validate_slabs(parsed)
    except SlabConfigError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return float(raw)
    except (TypeError, ValueError):
        return default


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _env_list(name: str, default: tuple[str, ...]) -> list[str]:
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return list(default)
    return [item.strip() for item in raw.split(",") if item.strip()]


DEFAULT_CORS_ORIGINS = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "http://localhost:4173",
    "http://127.0.0.1:4173",
)


@dataclass(frozen=True)
class Settings:
    data_dir: Path
    database_url: str
    timezone_name: str
    simulated_now: str | None
    currency: str

    default_tariff_name: str
    default_rate_type: str
    default_slabs: tuple[dict[str, Any], ...]
    default_flat_rate_per_kwh: float
    default_standing_charge_per_day: float
    default_tax_percent: float
    default_billing_start_day: int
    default_peak_start_hour: int
    default_peak_end_hour: int

    stale_reading_minutes: int
    nominal_voltage_v: float
    nominal_frequency_hz: float
    voltage_tolerance_percent: float

    # Accounts and sessions. Defaults are safe for local development; set
    # WATTXPENSE_COOKIE_SECURE=true when the app is served over HTTPS.
    session_ttl_hours: int
    cookie_secure: bool
    registration_enabled: bool
    login_max_attempts: int
    login_lockout_minutes: int

    # Hardware safety. Relay control is off for the whole installation by default and has
    # to be switched on explicitly; even then each device has its own opt-in flag. There
    # is deliberately no automatic switching path.
    relay_control_enabled: bool

    cors_origins: list[str] = field(default_factory=list)

    @property
    def tz(self) -> ZoneInfo:
        return timezone(self.timezone_name)

    def llm_settings(self) -> LLMSettings:
        """Read the optional local-model settings.

        There is deliberately no default model name: if it is unset the written
        explanation stays off rather than silently picking a model nobody chose.
        """
        return LLMSettings(
            enabled=_env_bool("WATTXPENSE_LLM_ENABLED", True),
            provider=os.environ.get("WATTXPENSE_LLM_PROVIDER", "ollama") or "ollama",
            base_url=os.environ.get("WATTXPENSE_LLM_BASE_URL", "http://127.0.0.1:11434"),
            model=(os.environ.get("WATTXPENSE_LLM_MODEL") or "").strip(),
            timeout_seconds=_env_float("WATTXPENSE_LLM_TIMEOUT_SECONDS", 60.0),
            temperature=_env_float("WATTXPENSE_LLM_TEMPERATURE", 0.2),
            keep_alive=os.environ.get("WATTXPENSE_LLM_KEEP_ALIVE", "5m") or "",
        )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    data_dir = Path(os.environ.get("WATTXPENSE_DATA_DIR", "./data")).expanduser()
    database_url = os.environ.get("WATTXPENSE_DATABASE_URL") or f"sqlite:///{data_dir / 'wattxpense.db'}"

    timezone_name = (os.environ.get("WATTXPENSE_TIMEZONE") or "Asia/Kolkata").strip() or "Asia/Kolkata"

    return Settings(
        data_dir=data_dir,
        database_url=database_url,
        timezone_name=timezone_name,
        simulated_now=(os.environ.get("WATTXPENSE_SIMULATED_NOW") or "").strip() or None,
        currency=os.environ.get("WATTXPENSE_CURRENCY", "INR"),
        default_tariff_name=os.environ.get("WATTXPENSE_DEFAULT_TARIFF_NAME", DEFAULT_TARIFF_NAME),
        default_rate_type=os.environ.get("WATTXPENSE_DEFAULT_RATE_TYPE", "slab"),
        default_slabs=_env_slabs("WATTXPENSE_DEFAULT_SLABS", DEFAULT_INDIA_DOMESTIC_SLABS),
        default_flat_rate_per_kwh=_env_float("WATTXPENSE_FLAT_RATE_PER_KWH", 0.0),
        default_standing_charge_per_day=_env_float("WATTXPENSE_STANDING_CHARGE_PER_DAY", 2.5),
        default_tax_percent=_env_float("WATTXPENSE_TAX_PERCENT", 5.0),
        default_billing_start_day=_env_int("WATTXPENSE_BILLING_START_DAY", 1),
        default_peak_start_hour=_env_int("WATTXPENSE_PEAK_START_HOUR", 18),
        default_peak_end_hour=_env_int("WATTXPENSE_PEAK_END_HOUR", 22),
        stale_reading_minutes=_env_int("WATTXPENSE_STALE_READING_MINUTES", 15),
        nominal_voltage_v=_env_float("WATTXPENSE_NOMINAL_VOLTAGE_V", 230.0),
        nominal_frequency_hz=_env_float("WATTXPENSE_NOMINAL_FREQUENCY_HZ", 50.0),
        voltage_tolerance_percent=_env_float("WATTXPENSE_VOLTAGE_TOLERANCE_PERCENT", 10.0),
        session_ttl_hours=_env_int("WATTXPENSE_SESSION_TTL_HOURS", 24 * 7),
        cookie_secure=_env_bool("WATTXPENSE_COOKIE_SECURE", False),
        registration_enabled=_env_bool("WATTXPENSE_REGISTRATION_ENABLED", True),
        login_max_attempts=_env_int("WATTXPENSE_LOGIN_MAX_ATTEMPTS", 5),
        login_lockout_minutes=_env_int("WATTXPENSE_LOGIN_LOCKOUT_MINUTES", 15),
        relay_control_enabled=_env_bool("WATTXPENSE_RELAY_CONTROL_ENABLED", False),
        cors_origins=_env_list("WATTXPENSE_CORS_ORIGINS", DEFAULT_CORS_ORIGINS),
    )


@lru_cache(maxsize=8)
def timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return ZoneInfo("UTC")
