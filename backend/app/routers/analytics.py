"""Usage and summary endpoints."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.services import analytics

router = APIRouter(prefix="/api/analytics", tags=["analytics"])


@router.get("/usage")
def usage(
    view: str = analytics.DEFAULT_VIEW,
    device_id: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    try:
        return analytics.usage(db, view=view, device_id=device_id)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/summary")
def summary(device_id: str | None = None, db: Session = Depends(get_db)) -> dict:
    return analytics.summary(db, device_id=device_id)
