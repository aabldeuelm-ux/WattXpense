"""UTC handling and the pinnable application clock.

Day boundaries, billing periods and "how stale is this reading" all depend on what the
server thinks now is. Keeping that in one place means a demo or a test can pin the clock
without any of the calculations learning about it.
"""

from __future__ import annotations

import os
from datetime import date, datetime, timezone as dt_timezone
from zoneinfo import ZoneInfo

from backend.app.config import get_settings, timezone as resolve_timezone


def utc_now() -> datetime:
    """The application's current time, in UTC, honouring `WATTXPENSE_SIMULATED_NOW`.

    The environment is read on every call rather than cached, because a test moves the
    pinned clock while the process is running. A pinned clock never rewrites timestamps on
    incoming readings; it only changes what the server considers the current time.
    """
    pinned = (os.environ.get("WATTXPENSE_SIMULATED_NOW") or "").strip()
    if pinned:
        parsed = parse_timestamp(pinned)
        if parsed is not None:
            return parsed.astimezone(dt_timezone.utc)
    return datetime.now(dt_timezone.utc).replace(microsecond=0)


def local_now() -> datetime:
    return utc_now().astimezone(tz())


def today() -> date:
    """Today in the configured local timezone, which is what a billing period is built from."""
    return local_now().date()


def tz() -> ZoneInfo:
    return resolve_timezone(get_settings().timezone_name)


def parse_timestamp(value: str | datetime) -> datetime | None:
    """Parse a timestamp to an aware UTC datetime, or return None if it is not usable.

    A naive timestamp is rejected rather than silently assumed to be UTC: assuming would
    put a reading from an offset-naive exporter into the wrong day, which then shows up
    as a phantom spike or a phantom gap.
    """
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
    else:
        return None

    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        return None
    return parsed.astimezone(dt_timezone.utc)


def to_local(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(tz())


def iso(value: datetime | None) -> str | None:
    """Render a stored timestamp the way the API reports it: UTC, second precision."""
    if value is None:
        return None
    if value.tzinfo is None:
        value = value.replace(tzinfo=dt_timezone.utc)
    return value.astimezone(dt_timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
