"""The local language model: provider interface, results and settings.

This is the only part of WattXpense that uses a language model, and it is optional. The
interface is deliberately small and every failure mode is a value rather than an
exception, so a dead or misbehaving model can never surface as a 500 on the dashboard.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

STATUS_OK = "ok"
STATUS_NOT_CONFIGURED = "not_configured"
STATUS_DISABLED = "disabled"
STATUS_UNREACHABLE = "unreachable"
STATUS_MODEL_MISSING = "model_missing"
STATUS_TIMEOUT = "timeout"
STATUS_MALFORMED = "malformed"
STATUS_UNGROUNDED = "ungrounded"
STATUS_ERROR = "error"

# The keys a model is allowed to return. Anything else it invents is dropped, so it can
# never leak into the dashboard as a field the app did not ask for.
ALLOWED_FIELDS = ("headline", "explanation", "suggestions", "caveats")


@dataclass(frozen=True)
class LLMSettings:
    enabled: bool = True
    provider: str = "ollama"
    base_url: str = "http://127.0.0.1:11434"
    model: str = ""
    timeout_seconds: float = 60.0
    temperature: float = 0.2
    keep_alive: str = "5m"


@dataclass
class LLMResult:
    """The outcome of talking to a model, successful or not."""

    status: str
    text: str = ""
    provider: str = ""
    model: str = ""
    payload: dict[str, Any] = field(default_factory=dict)
    reason: str = ""
    retryable: bool = False

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def as_dict(self) -> dict:
        return {
            "status": self.status,
            "provider": self.provider,
            "model": self.model,
            "reason": self.reason,
            "retryable": self.retryable,
        }


class LLMProvider:
    """Base class for a local model server.

    A provider only has to implement `complete`. `probe` has a portable default so a
    first-time author cannot forget the status check the dashboard depends on.
    """

    name = "base"

    def __init__(self, settings: LLMSettings) -> None:
        self.settings = settings
        self.model = settings.model

    def complete(self, system_prompt: str, user_prompt: str) -> LLMResult:  # pragma: no cover - abstract
        raise NotImplementedError("A provider must implement complete().")

    def probe(self) -> LLMResult:
        """Cheap "is this usable?" check that never depends on stored data.

        Refuses to call the model at all when it is switched off or unnamed, and
        otherwise falls back to generating a token, so a provider that has not written
        its own probe still answers the status endpoint rather than 500ing.
        """
        if not self.settings.enabled:
            return LLMResult(
                status=STATUS_DISABLED,
                provider=self.name,
                model=self.model,
                reason="The written explanation is switched off by configuration.",
            )
        if not (self.settings.model or "").strip():
            return LLMResult(
                status=STATUS_NOT_CONFIGURED,
                provider=self.name,
                model="",
                reason="No model name is configured.",
            )
        return self.complete("Reply with the single word: ready", "ready")

    def describe(self) -> dict:
        return {
            "provider": self.name,
            "model": self.model,
            "enabled": self.settings.enabled,
            "requires_api_key": False,
        }
