"""Forecast, anomaly, insight, and written-explanation endpoints.

The calculation endpoints (`forecast`, `anomalies`, `insights`) are pure arithmetic and
always available. Only `/explain` and `/status` involve the optional local model, and
both are built so that a model which is absent, broken, or ungrounded leaves the rest of
the dashboard working.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from backend.app.database import get_db
from backend.app.schemas import ExplainIn
from backend.app.services import analytics, llm_insights

router = APIRouter(prefix="/api/ai", tags=["ai"])


@router.get("/status")
def status() -> dict:
    return llm_insights.status()


@router.post("/explain")
def explain(
    payload: ExplainIn | None = None,
    db: Session = Depends(get_db),
) -> dict:
    request = payload or ExplainIn()
    return llm_insights.explain(
        db, device_id=request.device_id, horizon_days=request.horizon_days
    )


@router.get("/forecast")
def forecast(
    horizon_days: int = Query(default=7, ge=1, le=90),
    device_id: str | None = None,
    db: Session = Depends(get_db),
) -> dict:
    return analytics.forecast_panel(db, horizon_days=horizon_days, device_id=device_id)


@router.get("/anomalies")
def anomalies(device_id: str | None = None, db: Session = Depends(get_db)) -> dict:
    return analytics.anomalies_panel(db, device_id=device_id)


@router.get("/insights")
def insights(device_id: str | None = None, db: Session = Depends(get_db)) -> dict:
    return analytics.insights_panel(db, device_id=device_id)
