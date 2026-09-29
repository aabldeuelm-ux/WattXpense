"""Persisting validated samples.

Two properties matter more than speed here:

* **Idempotency.** Re-sending the same device and timestamp updates in place rather than
  double counting, so a retried batch, a replayed MQTT message or a re-run of the
  simulator cannot inflate a bill.
* **Batch isolation.** One impossible reading must not cost us the rest of the batch, so
  each sample is validated and recorded on its own and the rest carries on.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend.app.database import SessionLocal, session_scope
from backend.app.ingestion.base import (
    SIMULATED_SOURCES,
    ReadingSample,
    SampleRejected,
    flag_list,
    normalize_sample,
)
from backend.app.models import Device, Reading
from backend.app.timestamps import iso, utc_now

# Differences below this are the same measurement arriving twice, not a changed reading.
SAME_VALUE_TOLERANCE = 1e-6


@dataclass
class IngestResult:
    received: int = 0
    inserted: int = 0
    updated: int = 0
    duplicates: int = 0
    rejected: int = 0
    quality_flag_count: int = 0
    devices_registered: list[str] = field(default_factory=list)
    devices_touched: list[str] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "received": self.received,
            "inserted": self.inserted,
            "updated": self.updated,
            "duplicates": self.duplicates,
            "rejected": self.rejected,
            "quality_flag_count": self.quality_flag_count,
            "devices_registered": list(self.devices_registered),
            "devices_touched": list(self.devices_touched),
            "errors": list(self.errors),
        }


def ensure_device(
    db: Session,
    device_id: str,
    *,
    source: str | None = None,
    is_simulated: bool | None = None,
    name: str | None = None,
    location: str | None = None,
    is_primary: bool | None = None,
    nominal_voltage_v: float | None = None,
    notes: str | None = None,
) -> Device:
    """Fetch or create a device. The first device registered becomes the billed one."""
    device = db.get(Device, device_id)
    if device is None:
        # A single insert per device per batch, not one per sample.
        existing_primary = db.execute(select(Device).where(Device.is_primary.is_(True))).first()
        device = Device(
            id=device_id,
            name=name,
            location=location,
            source=source,
            is_simulated=is_simulated,
            is_primary=bool(is_primary) if is_primary is not None else existing_primary is None,
            nominal_voltage_v=nominal_voltage_v,
            notes=notes,
        )
        db.add(device)
        db.flush()
        return device

    if name is not None:
        device.name = name
    if location is not None:
        device.location = location
    if source is not None:
        device.source = source
    if is_simulated is not None:
        device.is_simulated = is_simulated
    if nominal_voltage_v is not None:
        device.nominal_voltage_v = nominal_voltage_v
    if notes is not None:
        device.notes = notes
    if is_primary is not None:
        device.is_primary = bool(is_primary)
    return device


def _same(a: float | None, b: float | None) -> bool:
    if a is None or b is None:
        return a is None and b is None
    return abs(float(a) - float(b)) <= SAME_VALUE_TOLERANCE


def _unchanged(existing: Reading, sample: ReadingSample) -> bool:
    return (
        _same(existing.voltage, sample.voltage)
        and _same(existing.current, sample.current)
        and _same(existing.power_w, sample.power_w)
        and _same(existing.energy_wh, sample.energy_wh)
    )


def _naive_utc(value: datetime) -> datetime:
    """SQLite drops tzinfo on the way in, so normalise on the way out too."""
    if value.tzinfo is None:
        from datetime import timezone as dt_timezone

        return value.replace(tzinfo=dt_timezone.utc)
    return value


def _refresh_device_stats(db: Session, device_ids: list[str]) -> None:
    """Derive the reporting interval and last-seen time from the data itself."""
    for device_id in device_ids:
        device = db.get(Device, device_id)
        if device is None:
            continue
        rows = db.execute(
            select(Reading.ts).where(Reading.device_id == device_id).order_by(Reading.ts)
        ).scalars()
        stamps = [r for r in rows if r is not None]
        if not stamps:
            continue
        device.last_seen = _naive_utc(stamps[-1])
        if len(stamps) >= 2:
            gaps = [
                (b - a).total_seconds()
                for a, b in zip(stamps, stamps[1:])
                if b > a
            ]
            if gaps:
                gaps.sort()
                middle = gaps[len(gaps) // 2]
                device.nominal_interval_seconds = int(round(middle))


def persist_samples(
    db: Session,
    samples: list[ReadingSample] | list[dict],
    *,
    source: str | None = None,
) -> IngestResult:
    """Store a batch, validating each sample independently."""
    result = IngestResult(received=len(samples))

    prepared: list[tuple[int, ReadingSample]] = []
    for index, raw in enumerate(samples):
        try:
            prepared.append((index, normalize_sample(raw, source)))
        except SampleRejected as exc:
            result.rejected += 1
            result.errors.append({"index": index, "reason": str(exc)})

    if not prepared:
        return result

    # One device lookup and one device insert per distinct device, not per sample.
    ordered_ids = list(dict.fromkeys(sample.device_id for _, sample in prepared))
    for device_id in ordered_ids:
        declared = {sample.is_simulated for _, sample in prepared if sample.device_id == device_id}
        existed = db.get(Device, device_id) is not None
        ensure_device(
            db,
            device_id,
            source=source,
            is_simulated=declared.pop() if len(declared) == 1 else None,
        )
        if not existed:
            result.devices_registered.append(device_id)
    result.devices_touched = ordered_ids

    # Load the rows this batch would overwrite in one query rather than per sample.
    pairs = {(sample.device_id, sample.ts) for _, sample in prepared}
    existing: dict[tuple[str, datetime], Reading] = {}
    for device_id in ordered_ids:
        for row in db.execute(select(Reading).where(Reading.device_id == device_id)).scalars():
            key = (device_id, _naive_utc(row.ts))
            if key in pairs:
                existing[key] = row

    for _, sample in prepared:
        key = (sample.device_id, sample.ts)
        flags = flag_list(sample.quality_flags)
        row = existing.get(key)
        if row is not None:
            if _unchanged(row, sample):
                result.duplicates += 1
                continue
            row.voltage = sample.voltage
            row.current = sample.current
            row.power_w = sample.power_w
            row.energy_wh = sample.energy_wh
            row.source = sample.source
            row.is_simulated = sample.is_simulated
            row.quality_flags = flags
            result.updated += 1
        else:
            db.add(
                Reading(
                    device_id=sample.device_id,
                    ts=sample.ts,
                    voltage=sample.voltage,
                    current=sample.current,
                    power_w=sample.power_w,
                    energy_wh=sample.energy_wh,
                    source=sample.source,
                    is_simulated=sample.is_simulated,
                    quality_flags=flags,
                )
            )
            result.inserted += 1
        if sample.quality_flags:
            result.quality_flag_count += 1

    db.flush()
    _refresh_device_stats(db, ordered_ids)
    return result


def ingest(
    samples: list[ReadingSample] | list[dict],
    *,
    source: str | None = None,
    commit: bool = True,
) -> IngestResult:
    """Convenience wrapper that owns its own session."""
    db = SessionLocal()
    try:
        result = persist_samples(db, samples, source=source)
        if commit:
            db.commit()
        return result
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


def delete_readings(db: Session, device_id: str) -> int:
    from sqlalchemy import delete

    count = db.execute(select(Reading.id).where(Reading.device_id == device_id)).scalars().all()
    if not count:
        return 0
    db.execute(delete(Reading).where(Reading.device_id == device_id))
    return len(count)


# ---------------------------------------------------------------------------------------
# Reads and serialisation
# ---------------------------------------------------------------------------------------


def get_device(db: Session, device_id: str) -> Device | None:
    return db.get(Device, device_id)


def device_exists(db: Session, device_id: str) -> bool:
    return db.get(Device, device_id) is not None


def list_devices(db: Session) -> list[Device]:
    """Oldest first, so the device that was primary by default stays first."""
    return list(
        db.execute(
            select(Device).order_by(Device.is_primary.desc(), Device.created_at, Device.id)
        ).scalars()
    )


def primary_device_id(db: Session) -> str | None:
    row = db.execute(
        select(Device.id).order_by(Device.is_primary.desc(), Device.created_at, Device.id)
    ).first()
    return row[0] if row else None


def simulated_reading_share(db: Session, device_id: str) -> float:
    """Share of a device's readings that are labelled simulated."""
    total = db.execute(
        select(func.count(Reading.id)).where(Reading.device_id == device_id)
    ).scalar_one()
    if not total:
        return 0.0
    simulated = db.execute(
        select(func.count(Reading.id)).where(Reading.device_id == device_id, Reading.is_simulated.is_(True))
    ).scalar_one()
    return simulated / total


def device_payload(db: Session, device: Device) -> dict:
    payload = device.as_dict()
    payload["created_at"] = iso(_naive_utc(device.created_at)) if device.created_at else None
    payload["last_seen"] = iso(_naive_utc(device.last_seen)) if device.last_seen else None
    payload["simulated_reading_share"] = simulated_reading_share(db, device.id)
    return payload


def reading_payload(row: Reading) -> dict:
    """One stored reading, with the two figures a raw row cannot supply on its own."""
    apparent = (row.voltage * row.current) if (row.voltage and row.current) else None
    factor = (row.power_w / apparent) if (apparent and row.power_w is not None) else None
    return {
        "id": row.id,
        "device_id": row.device_id,
        "ts": iso(_naive_utc(row.ts)),
        "voltage": row.voltage,
        "current": row.current,
        "power_w": row.power_w,
        "energy_wh": row.energy_wh,
        "apparent_power_va": round(apparent, 3) if apparent is not None else None,
        "derived_power_factor": round(factor, 4) if factor is not None else None,
        "source": row.source,
        "is_simulated": row.is_simulated,
        "quality_flags": row.flag_list(),
    }


def fetch_readings(
    db: Session, device_id: str, *, limit: int = 200, offset: int = 0
) -> tuple[list[Reading], int]:
    total = db.execute(
        select(func.count(Reading.id)).where(Reading.device_id == device_id)
    ).scalar_one()
    rows = list(
        db.execute(
            select(Reading)
            .where(Reading.device_id == device_id)
            .order_by(Reading.ts)
            .offset(offset)
            .limit(limit)
        ).scalars()
    )
    return rows, int(total)


def simulated_sources() -> set[str]:
    return set(SIMULATED_SOURCES)


def available_sources() -> list[dict]:
    """What the UI can offer as an ingestion source, and whether it is usable here."""
    return [
        {"id": "simulator", "label": "WattXpense simulator", "available": True, "simulated": True},
        {"id": "csv", "label": "CSV import", "available": True, "simulated": False},
        {"id": "mqtt", "label": "MQTT broker", "available": _mqtt_installed(), "simulated": False},
        {"id": "esp32", "label": "ESP32 on serial", "available": _serial_installed(), "simulated": False},
    ]


def _mqtt_installed() -> bool:
    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        return False
    return True


def _serial_installed() -> bool:
    try:
        import serial  # noqa: F401
    except ImportError:
        return False
    return True


__all__ = [
    "IngestResult",
    "ReadingSample",
    "available_sources",
    "delete_readings",
    "device_exists",
    "device_payload",
    "ensure_device",
    "fetch_readings",
    "get_device",
    "ingest",
    "iso",
    "list_devices",
    "persist_samples",
    "primary_device_id",
    "reading_payload",
    "session_scope",
    "simulated_reading_share",
    "simulated_sources",
    "utc_now",
]
