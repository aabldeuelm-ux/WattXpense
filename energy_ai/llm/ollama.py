"""Ollama provider.

Talks to a local Ollama server over HTTP with no API key and no credentials of any kind.
Every transport failure is translated into an `LLMResult` so nothing raises.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from energy_ai.llm.base import (
    ALLOWED_FIELDS,
    STATUS_DISABLED,
    STATUS_ERROR,
    STATUS_MALFORMED,
    STATUS_MODEL_MISSING,
    STATUS_NOT_CONFIGURED,
    STATUS_OK,
    STATUS_TIMEOUT,
    STATUS_UNREACHABLE,
    LLMProvider,
    LLMResult,
    LLMSettings,
)

# A model is "installed" if the tag matches exactly, or matches with an implicit
# ":latest". Ollama tags matter: qwen2.5:7b is not qwen2.5-coder:7b.
_MISSING_MODEL_HINT = re.compile(r"(model .*not found|try pulling it first|not installed)", re.IGNORECASE)


def _clean_text(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    return re.sub(r"\s+", " ", value).strip()


def _clean_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [text for text in (_clean_text(item) for item in value) if text]


def sanitize_payload(raw: Any) -> dict:
    """Keep only the four fields we asked for, with the types we asked for.

    A model that returns the wrong shape, or tries to return extra fields, gets them
    dropped rather than passed through to the dashboard.
    """
    if not isinstance(raw, dict):
        return {}
    if not any(field in raw for field in ALLOWED_FIELDS):
        return {}
    return {
        "headline": _clean_text(raw.get("headline")),
        "explanation": _clean_text(raw.get("explanation")),
        "suggestions": _clean_list(raw.get("suggestions")),
        "caveats": _clean_list(raw.get("caveats")),
    }


class OllamaProvider(LLMProvider):
    name = "ollama"

    def __init__(self, settings: LLMSettings, transport: httpx.BaseTransport | None = None) -> None:
        super().__init__(settings)
        self._transport = transport

    # -- internals -------------------------------------------------------------------

    @property
    def _base_url(self) -> str:
        return (self.settings.base_url or "").rstrip("/")

    def _client(self, timeout: float | None = None) -> httpx.Client:
        return httpx.Client(
            base_url=self._base_url,
            timeout=timeout if timeout is not None else self.settings.timeout_seconds,
            transport=self._transport,
        )

    def _unavailable(self) -> LLMResult | None:
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
                reason="No model name is configured, so no model was contacted.",
            )
        return None

    def _classify_transport_error(self, exc: Exception) -> LLMResult:
        detail = str(exc) or exc.__class__.__name__
        if isinstance(exc, httpx.TimeoutException):
            return LLMResult(
                status=STATUS_TIMEOUT,
                provider=self.name,
                model=self.model,
                reason="The local model took too long to respond.",
                retryable=True,
            )
        return LLMResult(
            status=STATUS_UNREACHABLE,
            provider=self.name,
            model=self.model,
            reason=f"The local model server at {self._base_url} is not responding ({detail}).",
            retryable=True,
        )

    def _missing_model(self, detail: str) -> LLMResult:
        return LLMResult(
            status=STATUS_MODEL_MISSING,
            provider=self.name,
            model=self.model,
            reason=f"Model '{self.model}' is not installed on the local server.",
            retryable=False,
        )

    # -- contract --------------------------------------------------------------------

    def complete(self, system_prompt: str, user_prompt: str) -> LLMResult:
        blocked = self._unavailable()
        if blocked is not None:
            return blocked

        body: dict[str, Any] = {
            "model": self.settings.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "stream": False,
            "format": "json",
            "options": {"temperature": self.settings.temperature},
        }
        if self.settings.keep_alive:
            body["keep_alive"] = self.settings.keep_alive

        try:
            with self._client() as client:
                response = client.post("/api/chat", json=body)
        except Exception as exc:  # noqa: BLE001 - transport failures are expected outcomes
            return self._classify_transport_error(exc)

        if response.status_code >= 400:
            detail = response.text or ""
            if response.status_code == 404 or _MISSING_MODEL_HINT.search(detail):
                return self._missing_model(detail)
            return LLMResult(
                status=STATUS_ERROR,
                provider=self.name,
                model=self.model,
                reason=f"The local model server returned {response.status_code}: {detail[:200]}",
            )

        try:
            content = response.json()["message"]["content"]
            parsed = json.loads(content)
        except Exception:  # noqa: BLE001 - any shape we did not expect is "malformed"
            return LLMResult(
                status=STATUS_MALFORMED,
                provider=self.name,
                model=self.model,
                payload={},
                reason="The model did not return the JSON structure that was requested.",
            )

        payload = sanitize_payload(parsed)
        if not payload:
            return LLMResult(
                status=STATUS_MALFORMED,
                provider=self.name,
                model=self.model,
                payload={},
                reason="The model did not return the fields that were requested.",
            )

        return LLMResult(status=STATUS_OK, text=content, provider=self.name, model=self.model, payload=payload)

    def probe(self) -> LLMResult:
        """Ask the server what it has installed. Never runs inference."""
        blocked = self._unavailable()
        if blocked is not None:
            return blocked

        try:
            with self._client(timeout=min(self.settings.timeout_seconds, 5.0)) as client:
                response = client.get("/api/tags")
        except Exception as exc:  # noqa: BLE001
            return self._classify_transport_error(exc)

        if response.status_code >= 400:
            detail = response.text or ""
            if response.status_code == 404 or _MISSING_MODEL_HINT.search(detail):
                return self._missing_model(detail)
            return LLMResult(
                status=STATUS_ERROR,
                provider=self.name,
                model=self.model,
                reason=f"The local model server returned {response.status_code}: {detail[:200]}",
            )

        try:
            names = [entry.get("name", "") for entry in response.json().get("models", [])]
        except Exception:  # noqa: BLE001
            return LLMResult(
                status=STATUS_MALFORMED,
                provider=self.name,
                model=self.model,
                reason="The local model server returned an unreadable model list.",
            )

        wanted = self.settings.model.strip()
        if any(_matches(wanted, name) for name in names):
            return LLMResult(status=STATUS_OK, provider=self.name, model=self.model)
        return self._missing_model(", ".join(names[:5]) or "nothing")

    def describe(self) -> dict:
        detail = super().describe()
        detail.update(
            {
                "base_url": self._base_url,
                "timeout_seconds": self.settings.timeout_seconds,
                "temperature": self.settings.temperature,
                "local": True,
            }
        )
        return detail


def _matches(wanted: str, installed: str) -> bool:
    if not wanted or not installed:
        return False
    if wanted == installed:
        return True
    return f"{wanted}:latest" == installed or f"{installed}:latest" == wanted
