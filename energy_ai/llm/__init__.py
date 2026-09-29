"""Optional local language model layer.

Providers are registered in exactly one place, `PROVIDERS`. To add another, implement
`complete()` (and `probe()` if the server can answer a cheap status check) in a new
module, register it here, and set `WATTXPENSE_LLM_PROVIDER`. Nothing in `backend/` or the
rest of `energy_ai/` needs to change, and no other module knows which provider is in use.
"""

from __future__ import annotations

import inspect

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
    STATUS_UNGROUNDED,
    STATUS_UNREACHABLE,
    LLMProvider,
    LLMResult,
    LLMSettings,
)
from energy_ai.llm.ollama import OllamaProvider, sanitize_payload

PROVIDERS: dict[str, type[LLMProvider]] = {
    OllamaProvider.name: OllamaProvider,
}


class UnknownProviderError(KeyError):
    """The configured provider is not registered."""


def build_provider(settings: LLMSettings, *, transport: httpx.BaseTransport | None = None) -> LLMProvider:
    """Construct the configured provider.

    `transport` is a test seam: it lets the suite drive a fake local server instead of
    requiring a real one to be running.
    """
    provider_class = PROVIDERS.get((settings.provider or "").strip())
    if provider_class is None:
        raise UnknownProviderError(
            f"Unknown LLM provider '{settings.provider}'. Registered providers: {', '.join(sorted(PROVIDERS))}."
        )
    if transport is not None and "transport" in inspect.signature(provider_class.__init__).parameters:
        return provider_class(settings, transport=transport)
    return provider_class(settings)


def status_help() -> dict:
    """What each status means, so the dashboard can explain itself without duplicating text."""
    return {
        STATUS_NOT_CONFIGURED: "No model name is configured. Set WATTXPENSE_LLM_MODEL and restart the server.",
        STATUS_DISABLED: "The written explanation is switched off by configuration.",
        STATUS_UNREACHABLE: "The local model server is not responding. Start it, then try again.",
        STATUS_MODEL_MISSING: "The configured model is not installed. Pull it, then try again.",
        STATUS_TIMEOUT: "The local model took too long to respond.",
        STATUS_MALFORMED: "The model output was discarded because it was not usable.",
        STATUS_UNGROUNDED: "The answer was discarded because it quoted a figure that is not in the data.",
        STATUS_ERROR: "The local model server reported an error.",
    }


__all__ = [
    "ALLOWED_FIELDS",
    "PROVIDERS",
    "LLMProvider",
    "LLMResult",
    "LLMSettings",
    "OllamaProvider",
    "STATUS_DISABLED",
    "STATUS_ERROR",
    "STATUS_MALFORMED",
    "STATUS_MODEL_MISSING",
    "STATUS_NOT_CONFIGURED",
    "STATUS_OK",
    "STATUS_TIMEOUT",
    "STATUS_UNGROUNDED",
    "STATUS_UNREACHABLE",
    "UnknownProviderError",
    "build_provider",
    "sanitize_payload",
    "status_help",
]
