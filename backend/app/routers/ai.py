"""Forecast, anomaly, insight, and written-explanation endpoints.

The calculation endpoints (`forecast`, `anomalies`, `insights`) are pure arithmetic and
always available. Only `/explain` and `/status` involve the optional local model, and
both are built so that a model which is absent, broken, or ungrounded leaves the rest of
the dashboard working. Every calculation is scoped to the signed-in account.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.auth import AuthContext, require_user
from backend.app.database import get_db
from backend.app.schemas import ExplainIn
from backend.app.services import analytics, llm_insights

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/status")
def status(_: AuthContext = Depends(require_user)) -> dict:
    return llm_insights.status()


@router.post("/explain")
def explain(
    payload: ExplainIn | None = None,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    request = payload or ExplainIn()
    return llm_insights.explain(
        db,
        device_id=request.device_id,
        horizon_days=request.horizon_days,
        owner_id=auth.user.id,
    )


@router.get("/forecast")
def forecast(
    horizon_days: int = Query(default=7, ge=1, le=90),
    device_id: str | None = None,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    return analytics.forecast_panel(
        db, horizon_days=horizon_days, device_id=device_id, owner_id=auth.user.id
    )


@router.get("/anomalies")
def anomalies(
    device_id: str | None = None,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    return analytics.anomalies_panel(db, device_id=device_id, owner_id=auth.user.id)


@router.get("/insights")
def insights(
    device_id: str | None = None,
    db: Session = Depends(get_db),
    auth: AuthContext = Depends(require_user),
) -> dict:
    return analytics.insights_panel(db, device_id=device_id, owner_id=auth.user.id)
