"""Optional local-model orchestration, with a calculated fallback that always works.

The model is handed a small block of figures the app has already computed and is asked to
describe them. Every failure path here ends in the same place: a status, a short reason,
and a summary built from the calculations alone, so the dashboard is never broken by a
model that is stopped, slow, or talking nonsense.
"""

from __future__ import annotations

from sqlalchemy.orm import Session

from backend.app.config import get_settings
from backend.app.services import analytics
from energy_ai.llm import (
    PROVIDERS,
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
    STATUS_UNGROUNDED,
    LLMResult,
    LLMSettings,
    build_provider,
    status_help,
)
from energy_ai.llm.prompt import (
    SYSTEM_PROMPT,
    build_context,
    build_user_prompt,
    fallback_narrative,
    find_ungrounded_measurements,
)

EMPTY_GENERATED = {"headline": "", "explanation": "", "suggestions": [], "caveats": []}

NO_DATA_MESSAGE = (
    "No device has reported yet, so there is nothing to explain. Record or simulate some "
    "readings first."
)


def _llm_settings() -> LLMSettings:
    # Read through the module-level name so the configuration can be swapped in tests.
    return get_settings().llm_settings()


def _generate(provider, context: dict) -> LLMResult:
    """Call the model, converting any exception into a normal failure result.

    A bug in a provider must never become a 500 on the dashboard.
    """
    name = getattr(provider, "name", "unknown")
    model = getattr(provider, "model", "") or ""
    try:
        return provider.complete(SYSTEM_PROMPT, build_user_prompt(context))
    except Exception as exc:  # noqa: BLE001 - deliberate: this is the crash firewall
        return LLMResult(
            status=STATUS_ERROR,
            provider=name,
            model=model,
            reason=f"The local model provider failed: {exc}",
        )


def _unknown_provider(settings: LLMSettings) -> dict | None:
    if settings.provider in PROVIDERS:
        return None
    return {
        "ai_available": False,
        "configured": False,
        "status": "unknown_provider",
        "provider": settings.provider,
        "model": settings.model or None,
        "message": (
            f"'{settings.provider}' is not a registered provider. Known providers: "
            f"{', '.join(sorted(PROVIDERS))}."
        ),
    }


def status() -> dict:
    """Is a local model usable? Never calls the model, and never raises."""
    settings = _llm_settings()
    base = {
        "provider": settings.provider,
        "model": settings.model or None,
        "enabled": bool(settings.enabled),
        "configured": bool(settings.enabled and (settings.model or "").strip()),
    }
    help_text = status_help()

    if not settings.enabled:
        return {
            **base,
            "ai_available": False,
            "status": STATUS_DISABLED,
            "message": help_text[STATUS_DISABLED],
        }
    if not (settings.model or "").strip():
        return {
            **base,
            "ai_available": False,
            "status": STATUS_NOT_CONFIGURED,
            "message": help_text[STATUS_NOT_CONFIGURED],
        }
    unknown = _unknown_provider(settings)
    if unknown is not None:
        return {**base, **unknown}

    try:
        provider = build_provider(settings)
    except Exception as exc:  # noqa: BLE001
        return {**base, "ai_available": False, "status": STATUS_ERROR, "message": str(exc)}

    try:
        result = provider.probe()
    except Exception as exc:  # noqa: BLE001
        return {**base, "ai_available": False, "status": STATUS_ERROR, "message": str(exc)}

    return {
        **base,
        "ai_available": bool(result.ok),
        "status": result.status,
        "message": "" if result.ok else (result.reason or help_text.get(result.status, "The model is not available.")),
        "retryable": bool(result.retryable),
    }


def _generated_text(payload: dict) -> str:
    """Everything the model said, flattened for the grounding check."""
    parts: list[str] = [str(payload.get("headline") or ""), str(payload.get("explanation") or "")]
    parts.extend(str(item) for item in (payload.get("suggestions") or []))
    parts.extend(str(item) for item in (payload.get("caveats") or []))
    return "\n".join(part for part in parts if part)


def explain(
    db: Session, *, device_id: str | None = None, horizon_days: int = 7, owner_id: int | None = None
) -> dict:
    """A written explanation of the current figures, plus the figures it was built from."""
    device = analytics.pick_device(db, device_id, owner_id)
    if device is None:
        return {
            "status": "no_data",
            "ai_available": False,
            "source": "calculated",
            "generated": dict(EMPTY_GENERATED),
            "calculated": {},
            "warnings": [],
            "message": NO_DATA_MESSAGE,
        }

    summary = analytics.summary(db, device_id=device.id, owner_id=owner_id)
    insights = analytics.insights_panel(db, device_id=device.id, owner_id=owner_id)
    forecast = analytics.forecast_panel(db, horizon_days=horizon_days, device_id=device.id, owner_id=owner_id)
    anomalies = analytics.anomalies_panel(db, device_id=device.id, owner_id=owner_id)

    context = build_context(
        summary=summary,
        insights=insights,
        forecast=forecast,
        anomalies=anomalies,
        data_label=summary.get("data_label", "Metered"),
    )
    fallback = fallback_narrative(context)

    body: dict = {
        "generated": fallback,
        "calculated": context,
        "source": "calculated",
        "ai_available": False,
        "status": STATUS_NOT_CONFIGURED,
        "model": None,
        "warnings": [],
        "message": "",
    }

    settings = _llm_settings()
    if not settings.enabled:
        body["status"] = STATUS_DISABLED
        body["message"] = status_help()[STATUS_DISABLED]
        return body
    if not (settings.model or "").strip():
        body["status"] = STATUS_NOT_CONFIGURED
        body["message"] = status_help()[STATUS_NOT_CONFIGURED]
        return body

    unknown = _unknown_provider(settings)
    if unknown is not None:
        body["status"] = unknown["status"]
        body["message"] = unknown["message"]
        return body

    try:
        provider = build_provider(settings)
    except Exception as exc:  # noqa: BLE001
        body["status"] = STATUS_ERROR
        body["message"] = str(exc)
        return body

    result = _generate(provider, context)
    body["model"] = result.model or settings.model

    if not result.ok:
        body["status"] = result.status
        body["message"] = result.reason or status_help().get(result.status, "The model is not available.")
        return body

    # The model answered. Its prose is checked before any of it reaches the dashboard.
    ungrounded = find_ungrounded_measurements(_generated_text(result.payload), context)
    if ungrounded:
        body["status"] = STATUS_UNGROUNDED
        body["ai_available"] = True
        body["message"] = (
            "The model's answer was discarded because it quoted a figure that is not in the "
            "calculated data."
        )
        body["warnings"] = [
            f"The model's answer was discarded and replaced with the calculated summary, because "
            f"it quoted {', '.join(ungrounded[:5])}, which is not a figure this app calculated."
        ]
        return body

    body["status"] = STATUS_OK
    body["ai_available"] = True
    body["source"] = "model"
    body["generated"] = result.payload
    body["message"] = "Written by the local model from the figures below. Every number is WattXpense's."
    return body
