"""Tariff configuration and bill preview.

A tariff is operator input, so every field is validated before it is stored: a slab
structure that leaves consumption unpriced, or a peak rate that is cheaper than the
off-peak rate, is refused rather than quietly billed. Tariffs are per account.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.app.auth import AuthContext, require_user
from backend.app.database import get_db
from backend.app.models import Tariff
from backend.app.services import billing, tariffs
from backend.app.schemas import TariffIn

router = APIRouter(prefix="/api/billing", tags=["billing"])


@router.get("/tariff")
def get_tariff(
    db: Session = Depends(get_db), auth: AuthContext = Depends(require_user)
) -> dict:
    return {"tariff": tariffs.as_dict(tariffs.get_active(db, auth.user.id))}


@router.get("/tariffs")
def list_tariffs(
    db: Session = Depends(get_db), auth: AuthContext = Depends(require_user)
) -> dict:
    return {"tariffs": [tariffs.as_dict(row) for row in tariffs.list_tariffs(db, auth.user.id)]}


@router.post("/tariff")
def set_tariff(
    payload: TariffIn,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    """Store a new tariff and make it the active one for this account.

    The previous tariff is kept rather than overwritten so a pricing change is visible
    rather than silent.
    """
    if not payload.name.strip():
        raise HTTPException(status_code=422, detail="A tariff needs a name")

    row = Tariff(
        owner_id=auth.user.id,
        name=payload.name.strip(),
        rate_type=payload.rate_type,
        currency=payload.currency.upper(),
        flat_rate_per_kwh=payload.flat_rate_per_kwh,
        peak_rate_per_kwh=payload.peak_rate_per_kwh,
        offpeak_rate_per_kwh=payload.offpeak_rate_per_kwh,
        peak_start_hour=payload.peak_start_hour,
        peak_end_hour=payload.peak_end_hour,
        standing_charge_per_day=payload.standing_charge_per_day,
        tax_percent=payload.tax_percent,
        billing_start_day=payload.billing_start_day,
        slabs=json.dumps(payload.slab_dicts()) if payload.rate_type == "slab" else None,
        is_active=payload.is_active,
    )
    db.add(row)
    db.flush()

    if payload.is_active:
        others = db.execute(
            select(Tariff).where(Tariff.owner_id == auth.user.id, Tariff.id != row.id)
        ).scalars()
        for other in others:
            other.is_active = False
    db.commit()
    db.refresh(row)
    return {"tariff": tariffs.as_dict(row)}


@router.get("/preview")
def preview(
    energy_kwh: float = Query(..., ge=0),
    days: int = Query(..., ge=0, le=366),
    peak_share: float | None = Query(default=None, ge=0, le=1),
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    """Price a hypothetical quantity of energy, itemised.

    `peak_share` lets a caller ask "what if 30% of this landed in the peak window"
    without having to know how the hours are defined.
    """
    config = tariffs.get_config(db, auth.user.id)
    peak_kwh = offpeak_kwh = None
    if peak_share is not None:
        peak_kwh = round(energy_kwh * peak_share, 4)
        offpeak_kwh = round(energy_kwh - peak_kwh, 4)
    estimate = billing.estimate_bill(
        config,
        energy_kwh=energy_kwh,
        days=days,
        peak_kwh=peak_kwh,
        offpeak_kwh=offpeak_kwh,
    )
    return {
        "estimate": estimate,
        "tariff": tariffs.as_dict(tariffs.get_active(db, auth.user.id)),
        "effective_rate": tariffs.effective_rate(config, energy_kwh or None),
    }
