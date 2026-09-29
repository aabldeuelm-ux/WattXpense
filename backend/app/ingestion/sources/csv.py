"""CSV import.

The CSV is a source, not a special case: rows are normalised through exactly the same
path as MQTT messages, so a bad row in a file is reported the same way as a bad message
on a broker.
"""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from backend.app.ingestion.base import SIMULATED_SOURCES

TIMESTAMP_FIELDS = ("ts", "timestamp", "time", "datetime", "recorded_at")
NUMERIC_FIELDS = ("voltage", "current", "power_w", "energy_wh", "watts", "energy")


class CsvImportError(ValueError):
    """The file could not be read as a readings export at all."""


def _pick(row: dict[str, Any], names: tuple[str, ...]) -> Any:
    for name in names:
        if name in row and row[name] not in (None, ""):
            return row[name]
    return None


def parse_csv(path: str | Path, *, device_id: str | None = None, source: str = "csv") -> list[dict]:
    """Read a readings CSV into raw payloads for the shared ingestion path."""
    target = Path(path)
    if not target.exists() or not target.is_file():
        raise CsvImportError(f"No CSV file at {target}")

    rows: list[dict] = []
    with target.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        fieldnames = [name.strip().lower() for name in (reader.fieldnames or [])]
        if not fieldnames:
            raise CsvImportError("The CSV has no header row.")
        if not any(name in TIMESTAMP_FIELDS for name in fieldnames):
            raise CsvImportError("The CSV needs a timestamp column named one of: " + ", ".join(TIMESTAMP_FIELDS))

        for raw in reader:
            row = {(key or "").strip().lower(): value for key, value in raw.items() if key}
            payload: dict[str, Any] = {
                "device_id": _pick(row, ("device_id", "device", "meter")) or device_id,
                "ts": _pick(row, TIMESTAMP_FIELDS),
                "source": row.get("source") or source,
            }
            for name in NUMERIC_FIELDS:
                value = row.get(name)
                if value not in (None, ""):
                    payload["power_w" if name == "watts" else "energy" if name == "energy" else name] = value
            if payload.get("source") in SIMULATED_SOURCES:
                payload["is_simulated"] = True
            rows.append(payload)
    return rows
