"""Device, reading, and ingestion endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy.orm import Session

from backend.app import timestamps
from backend.app.config import get_settings
from backend.app.database import get_db
from backend.app.ingestion import service
from backend.app.ingestion.base import (
    SIMULATED_SOURCES,
    SampleRejected,
    derive_energy,
    derive_missing,
    normalize_sample,
)
from backend.app.ingestion.sources.csv import CsvImportError, parse_csv
from backend.app.ingestion.sources.mqtt import MqttConfig, poll_once
from backend.app.schemas import DeviceIn, ManualReadingIn, ReadingsBatchIn
from backend.app.services import analytics

router = APIRouter(prefix="/api", tags=["core"])


@router.get("/health")
def health() -> dict:
    settings = get_settings()
    return {
        "status": "ok",
        "timezone": settings.timezone_name,
        "server_time": timestamps.iso(timestamps.local_now()),
    }


@router.get("/meta")
def meta(db: Session = Depends(get_db)) -> dict:
    """What the frontend needs to describe this install before asking for any data."""
    settings = get_settings()
    device = analytics.pick_device(db, None)
    return {
        "timezone": settings.timezone_name,
        "currency": settings.currency,
        "nominal_voltage_v": settings.nominal_voltage_v,
        "views": list(analytics.VIEW_DAYS),
        "default_view": analytics.DEFAULT_VIEW,
        "simulated_sources": sorted(SIMULATED_SOURCES),
        "has_device": device is not None,
        "device_id": device.id if device else None,
        "data_label": analytics.data_label(db, device) if device is not None else None,
    }


# ---------------------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------------------


@router.get("/devices")
def list_devices(db: Session = Depends(get_db)) -> dict:
    return {
        "devices": [service.device_payload(db, device) for device in service.list_devices(db)],
        "primary": service.primary_device_id(db),
    }


@router.post("/devices")
def create_device(payload: DeviceIn, db: Session = Depends(get_db)) -> dict:
    fields = payload.model_dump(exclude_none=True)
    device_id = fields.pop("id")
    device = service.ensure_device(db, device_id, **fields)
    db.commit()
    return {"device": service.device_payload(db, device)}


# ---------------------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------------------


@router.get("/readings")
def list_readings(
    device_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
) -> dict:
    device = analytics.pick_device(db, device_id)
    if device is None:
        raise HTTPException(status_code=404, detail="No device has reported yet")
    rows, total = service.fetch_readings(db, device.id, limit=limit, offset=offset)
    return {
        "readings": [service.reading_payload(row) for row in rows],
        "total": total,
        "device_id": device.id,
        "data_label": analytics.data_label(db, device),
    }


@router.post("/readings")
def post_readings(payload: ReadingsBatchIn, db: Session = Depends(get_db)) -> dict:
    if not payload.readings:
        raise HTTPException(status_code=422, detail="No readings supplied")
    # Shape errors are a 422 for the whole request; anything physically impossible is
    # left to the ingestion layer, which rejects it per sample and keeps the rest.
    rows = [entry.model_dump() for entry in payload.readings]
    result = service.persist_samples(db, rows, source=payload.source)
    db.commit()
    return result.as_dict()


@router.post("/readings/manual")
def post_manual_reading(payload: ManualReadingIn, db: Session = Depends(get_db)) -> dict:
    device = service.get_device(db, payload.device_id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Unknown device '{payload.device_id}'")
    try:
        sample = normalize_sample(payload.model_dump(), source="manual")
    except SampleRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    derive_missing(sample)
    derive_energy(sample, device.nominal_interval_seconds)
    result = service.persist_samples(db, [sample], source="manual")
    db.commit()
    return {
        **result.as_dict(),
        "derived": {
            "voltage": sample.voltage,
            "current": sample.current,
            "power_w": sample.power_w,
            "energy_wh": sample.energy_wh,
            "interval_seconds": device.nominal_interval_seconds,
        },
    }


@router.post("/readings/import-csv")
def import_csv(
    path: str = Query(...),
    device_id: str | None = None,
    source: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    try:
        rows = parse_csv(path, device_id=device_id, source=source or "csv")
    except CsvImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = service.persist_samples(db, rows, source=source or "csv")
    db.commit()
    return result.as_dict()


@router.delete("/readings")
def delete_readings(device_id: str, db: Session = Depends(get_db)) -> dict:
    if not service.device_exists(db, device_id):
        raise HTTPException(status_code=404, detail=f"Unknown device '{device_id}'")
    removed = service.delete_readings(db, device_id)
    db.commit()
    return {"deleted": removed, "device_id": device_id}


# ---------------------------------------------------------------------------------------
# Ingestion sources
# ---------------------------------------------------------------------------------------


@router.get("/ingestion/sources")
def ingestion_sources() -> dict:
    return {
        "sources": service.available_sources(),
        "simulated_sources": sorted(SIMULATED_SOURCES),
    }


@router.post("/ingestion/mqtt/poll")
def mqtt_poll(db: Session = Depends(get_db)) -> dict:
    result = poll_once(MqttConfig.from_env())
    if result.status != "ok":
        # An unreachable broker is a normal state for an install that does not use one.
        raise HTTPException(status_code=502, detail=result.message or result.status)
    stored = service.persist_samples(db, result.samples)
    db.commit()
    return {**result.as_dict(), **stored.as_dict()}
