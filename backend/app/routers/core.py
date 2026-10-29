"""Device, reading, hardware telemetry, relay, and ingestion endpoints.

Every account-facing endpoint is authenticated and scoped to the signed-in user. Hardware
uses a separate bearer token that identifies exactly one device, and that device belongs
to exactly one account. The relay endpoints record a command; they never claim that a
physical relay moved without an acknowledgement from the device.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app import timestamps
from backend.app.auth import AuthContext, DevicePrincipal, require_device, require_user
from backend.app.config import get_settings
from backend.app.database import get_db
from backend.app.ingestion import service
from backend.app.ingestion.adapters import AdapterContext, Esp32Adapter
from backend.app.ingestion.base import (
    SIMULATED_SOURCES,
    SampleRejected,
    derive_energy,
    derive_missing,
    normalize_sample,
)
from backend.app.ingestion.sources.csv import CsvImportError, parse_csv
from backend.app.ingestion.sources.mqtt import MqttConfig, poll_once
from backend.app.models import Device
from backend.app.schemas import (
    DeviceIn,
    ManualReadingIn,
    ReadingsBatchIn,
    RelayAckIn,
    RelayCommandIn,
    TelemetryBatchIn,
)
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
def meta(db: Session = Depends(get_db), auth: AuthContext = Depends(require_user)) -> dict:
    """What the frontend needs to describe this account before asking for any data."""
    settings = get_settings()
    device = analytics.pick_device(db, None, auth.user.id)
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
        "user": auth.user.as_dict(),
        "registration_enabled": settings.registration_enabled,
        "relay_control_enabled": settings.relay_control_enabled,
    }


# ---------------------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------------------


@router.get("/devices")
def list_devices(db: Session = Depends(get_db), auth: AuthContext = Depends(require_user)) -> dict:
    return {
        "devices": [service.device_payload(db, device) for device in service.list_devices(db, auth.user.id)],
        "primary": service.primary_device_id(db, auth.user.id),
    }


@router.post("/devices")
def create_device(
    payload: DeviceIn,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    fields = payload.model_dump(exclude_none=True)
    device_id = fields.pop("id")
    rotate_token = bool(fields.pop("rotate_token", False))
    calibration = fields.pop("calibration", None)
    gpio_config = fields.pop("gpio_config", None)
    if calibration is not None:
        fields["calibration_json"] = json.dumps(calibration)
    if gpio_config is not None:
        fields["gpio_config_json"] = json.dumps(gpio_config)

    existing = db.get(Device, device_id)
    if existing is not None and existing.owner_id != auth.user.id:
        # Do not reveal whether the id belongs to another account or is a legacy row.
        raise HTTPException(
            status_code=409,
            detail=(
                "That device id is already in use. Existing unowned data must be claimed "
                "explicitly by an operator before it can be attached to an account."
            ),
        )

    try:
        device = service.ensure_device(db, device_id, owner_id=auth.user.id, **fields)
    except service.OwnershipError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if fields.get("is_primary"):
        for other in db.execute(
            select(Device).where(Device.owner_id == auth.user.id, Device.id != device.id)
        ).scalars():
            other.is_primary = False

    token = None
    if existing is None or rotate_token or not device.api_token_hash:
        token = service.issue_device_token(db, device)
    db.commit()
    response = {"device": service.device_payload(db, device)}
    if token:
        response["device_token"] = token
        response["token_note"] = (
            "Store this device token now. It is shown once and is the only credential the "
            "device needs. Send it as Authorization: Bearer <token> to /api/device/telemetry."
        )
    return response


@router.post("/devices/{device_id}/token")
def rotate_device_token(
    device_id: str,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    device = service.get_device(db, device_id, auth.user.id)
    if device is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    token = service.issue_device_token(db, device)
    db.commit()
    return {
        "device_id": device.id,
        "device_token": token,
        "token_note": "The previous device token no longer works. Store this one now.",
    }


# ---------------------------------------------------------------------------------------
# Readings
# ---------------------------------------------------------------------------------------


@router.get("/readings")
def list_readings(
    device_id: str | None = None,
    limit: int = Query(default=200, ge=1, le=5000),
    offset: int = Query(default=0, ge=0),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    device = analytics.pick_device(db, device_id, auth.user.id)
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
def post_readings(
    payload: ReadingsBatchIn,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    if not payload.readings:
        raise HTTPException(status_code=422, detail="No readings supplied")
    # Shape errors are a 422 for the whole request; anything physically impossible is
    # left to the ingestion layer, which rejects it per sample and keeps the rest.
    rows = [entry.model_dump() for entry in payload.readings]
    result = service.persist_samples(db, rows, source=payload.source, owner_id=auth.user.id)
    db.commit()
    return result.as_dict()


@router.post("/readings/manual")
def post_manual_reading(
    payload: ManualReadingIn,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    device = service.get_device(db, payload.device_id, auth.user.id)
    if device is None:
        raise HTTPException(status_code=404, detail=f"Unknown device '{payload.device_id}'")
    try:
        sample = normalize_sample(payload.model_dump(), source="manual")
    except SampleRejected as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    derive_missing(sample)
    derive_energy(sample, device.nominal_interval_seconds)
    result = service.persist_samples(db, [sample], source="manual", owner_id=auth.user.id)
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
    auth: AuthContext = Depends(require_user),
) -> dict:
    # A brand-new device id is registered to the importer by `persist_samples`. An id that
    # already exists but belongs to someone else is refused rather than taken over.
    if device_id is not None:
        existing = db.get(Device, device_id)
        if existing is not None and existing.owner_id != auth.user.id:
            raise HTTPException(status_code=409, detail=f"Device '{device_id}' is not available")
    try:
        rows = parse_csv(path, device_id=device_id, source=source or "csv")
    except CsvImportError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    result = service.persist_samples(db, rows, source=source or "csv", owner_id=auth.user.id)
    db.commit()
    return result.as_dict()


@router.delete("/readings")
def delete_readings(
    device_id: str,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    if not service.device_exists(db, device_id, auth.user.id):
        raise HTTPException(status_code=404, detail=f"Unknown device '{device_id}'")
    removed = service.delete_readings(db, device_id)
    db.commit()
    return {"deleted": removed, "device_id": device_id}


# ---------------------------------------------------------------------------------------
# Ingestion sources
# ---------------------------------------------------------------------------------------


@router.get("/ingestion/sources")
def ingestion_sources(_: AuthContext = Depends(require_user)) -> dict:
    return {
        "sources": service.available_sources(),
        "simulated_sources": sorted(SIMULATED_SOURCES),
    }


@router.post("/ingestion/mqtt/poll")
def mqtt_poll(db: Session = Depends(get_db), auth: AuthContext = Depends(require_user)) -> dict:
    result = poll_once(MqttConfig.from_env())
    if result.status != "ok":
        # An unreachable broker is a normal state for an install that does not use one.
        raise HTTPException(status_code=502, detail=result.message or result.status)
    stored = service.persist_samples(db, result.samples, owner_id=auth.user.id)
    db.commit()
    return {**result.as_dict(), **stored.as_dict()}


# ---------------------------------------------------------------------------------------
# Hardware telemetry (device bearer token)
# ---------------------------------------------------------------------------------------


@router.post("/device/telemetry")
def device_telemetry(
    payload: TelemetryBatchIn,
    db: Session = Depends(get_db),
    principal: DevicePrincipal = Depends(require_device),
) -> dict:
    """The ESP32-facing ingestion endpoint.

    The token decides the device id. A payload that names a different device is refused,
    so a compromised device cannot write into another account by guessing an id.
    """
    if not payload.readings:
        raise HTTPException(status_code=422, detail="No readings supplied")

    device = principal.device
    interval = device.sampling_interval_seconds or device.nominal_interval_seconds
    context = AdapterContext(device_id=device.id, sampling_interval_seconds=interval)
    adapter = Esp32Adapter()

    prepared = []
    errors = []
    for index, entry in enumerate(payload.readings):
        if entry.device_id and entry.device_id != device.id:
            errors.append({"index": index, "reason": "device_id does not match the device token"})
            continue
        try:
            prepared.append(adapter.prepare(entry.model_dump(), context))
        except SampleRejected as exc:
            errors.append({"index": index, "reason": str(exc)})

    result = service.persist_samples(db, prepared, source="esp32", owner_id=device.owner_id)
    db.commit()
    body = result.as_dict()
    body["received"] = len(payload.readings)
    body["rejected"] += len(errors)
    body["errors"] = errors + body["errors"]
    body["device_id"] = device.id
    body["note"] = (
        "Hardware samples are validated by the same ingestion path as simulated data. "
        "Power derived from voltage and current is flagged as an estimate."
    )
    return body


# ---------------------------------------------------------------------------------------
# Relay
# ---------------------------------------------------------------------------------------


def _relay_payload(device: Device) -> dict:
    return {
        "device_id": device.id,
        "relay_enabled": bool(device.relay_enabled),
        "relay_state": device.relay_state,
        "relay_updated_at": timestamps.iso(device.relay_updated_at) if device.relay_updated_at else None,
        "acknowledged": device.relay_state in {"on", "off", "unknown", "error"},
        "note": (
            "A pending state means the command was recorded but no hardware has confirmed it. "
            "WattXpense never switches a relay automatically."
        ),
    }


@router.get("/devices/{device_id}/relay")
def get_relay(
    device_id: str,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    device = service.get_device(db, device_id, auth.user.id)
    if device is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    return {"relay": _relay_payload(device), "control_available": get_settings().relay_control_enabled}


@router.post("/devices/{device_id}/relay")
def command_relay(
    device_id: str,
    payload: RelayCommandIn,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    settings = get_settings()
    if not settings.relay_control_enabled:
        raise HTTPException(status_code=409, detail="Relay control is disabled for this install")
    device = service.get_device(db, device_id, auth.user.id)
    if device is None:
        raise HTTPException(status_code=404, detail="Unknown device")
    if not device.relay_enabled:
        raise HTTPException(
            status_code=409,
            detail="Relay control is not enabled for this device. Enable it deliberately first.",
        )
    if not payload.confirm:
        raise HTTPException(status_code=422, detail="Confirm the relay command explicitly")

    device.relay_state = f"pending_{payload.state}"
    device.relay_updated_at = timestamps.utc_now()
    db.commit()
    return {
        "relay": _relay_payload(device),
        "message": (
            "Command recorded. It is not a confirmation that the relay moved; wait for the "
            "device to acknowledge the new state."
        ),
    }


@router.get("/device/relay")
def device_relay(principal: DevicePrincipal = Depends(require_device)) -> dict:
    """Firmware polling endpoint: read the pending command and current known state."""
    return {"relay": _relay_payload(principal.device)}


@router.post("/device/relay/ack")
def device_relay_ack(
    payload: RelayAckIn,
    db: Session = Depends(get_db),
    principal: DevicePrincipal = Depends(require_device),
) -> dict:
    device = principal.device
    device.relay_state = payload.state
    device.relay_updated_at = timestamps.utc_now()
    db.commit()
    return {"relay": _relay_payload(device), "acknowledged": True}
