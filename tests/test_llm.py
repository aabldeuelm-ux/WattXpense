"""Tests for the optional local-model explanation layer.

The recurring theme, matching the rest of the AI test suite: a model is a nice-to-have and must
never be able to damage the dashboard or overstate what is known. These tests pin down that the
numbers stay the app's own, that a dead or misbehaving model degrades cleanly, and that a model
quoting a figure that is not in the data gets discarded.
"""

from __future__ import annotations

import json

import httpx
import pytest

from backend.app.services import llm_insights
from energy_ai.llm import (
    PROVIDERS,
    STATUS_DISABLED,
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
    OllamaProvider,
    build_provider,
)
from energy_ai.llm.prompt import (
    build_context,
    build_user_prompt,
    fallback_narrative,
    find_ungrounded_measurements,
)
from tests.conftest import build_readings, post_readings

MODEL = "some-local-model"


def llm_settings(**overrides) -> LLMSettings:
    base = dict(
        enabled=True,
        provider="ollama",
        base_url="http://127.0.0.1:11434",
        model=MODEL,
        timeout_seconds=5.0,
        temperature=0.2,
        keep_alive="",
    )
    base.update(overrides)
    return LLMSettings(**base)


def configure_model(monkeypatch, **overrides):
    """Point the service at a configured model, as if .env had been set."""
    settings = llm_settings(**overrides)
    monkeypatch.setattr(llm_insights, "get_settings", lambda: _SettingsShim(settings))
    return settings


class _SettingsShim:
    """Minimal stand-in so tests can swap in LLM settings without touching the database config."""

    def __init__(self, llm: LLMSettings):
        self._llm = llm

    def llm_settings(self) -> LLMSettings:
        return self._llm


def install_model(monkeypatch, handler):
    """Make the service build a provider that talks to a fake local server."""

    class Shim:
        def llm_settings(self) -> LLMSettings:
            return llm_settings()

    monkeypatch.setattr(llm_insights, "get_settings", lambda: Shim())

    def fake_build_provider(settings, **kwargs):
        return build_provider(settings, transport=httpx.MockTransport(handler))

    monkeypatch.setattr(llm_insights, "build_provider", fake_build_provider)


def tags_with(*names):
    return lambda request: httpx.Response(200, json={"models": [{"name": name} for name in names]})


def model_reply(**overrides) -> dict:
    payload = {
        "headline": "A steady overnight baseline is the main thing to look at",
        "explanation": "The figures show a load that never drops to zero overnight.",
        "suggestions": ["Put standby devices on smart plugs", "Run the dishwasher outside the evening peak"],
        "caveats": [],
    }
    payload.update(overrides)
    return payload


def reply_with(payload: dict):
    return lambda request: httpx.Response(200, json={"message": {"content": json.dumps(payload)}})


def seed_history(client, device_id="sim-main-01", days=25, **kwargs):
    """A flat, fully covered load, which is enough history for insights to report something."""
    rows = build_readings(device_id, "2026-09-01T00:00:00Z", days=days, watts=250.0, **kwargs)
    return post_readings(client, rows)


# ---------------------------------------------------------------------------------------
# Provider: transport handling
# ---------------------------------------------------------------------------------------


class TestOllamaProvider:
    def test_sends_model_and_messages_from_configuration(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"message": {"content": json.dumps(model_reply())}})

        provider = OllamaProvider(llm_settings(), transport=httpx.MockTransport(handler))
        result = provider.complete("system text", "user text")

        assert result.status == STATUS_OK
        assert seen["url"] == "http://127.0.0.1:11434/api/chat"
        assert seen["body"]["model"] == MODEL
        assert seen["body"]["stream"] is False
        assert [m["role"] for m in seen["body"]["messages"]] == ["system", "user"]
        assert seen["body"]["messages"][0]["content"] == "system text"
        assert seen["body"]["options"]["temperature"] == 0.2

    def test_base_url_trailing_slash_does_not_double_up(self):
        seen = {}

        def handler(request):
            seen["url"] = str(request.url)
            return httpx.Response(200, json={"message": {"content": json.dumps(model_reply())}})

        OllamaProvider(llm_settings(base_url="http://127.0.0.1:11434/"), transport=httpx.MockTransport(handler)).complete(
            "s", "u"
        )
        assert seen["url"] == "http://127.0.0.1:11434/api/chat"

    def test_never_raises_when_the_server_is_not_running(self):
        def handler(request):
            raise httpx.ConnectError("connection refused")

        result = OllamaProvider(llm_settings(), transport=httpx.MockTransport(handler)).complete("s", "u")

        assert result.status == STATUS_UNREACHABLE
        assert result.retryable is True
        assert "not responding" in result.reason

    def test_reports_a_timeout_without_raising(self):
        def handler(request):
            raise httpx.ReadTimeout("too slow")

        result = OllamaProvider(llm_settings(timeout_seconds=0.01), transport=httpx.MockTransport(handler)).complete(
            "s", "u"
        )

        assert result.status == STATUS_TIMEOUT
        assert "too long" in result.reason

    def test_missing_model_is_reported_distinctly(self):
        result = OllamaProvider(
            llm_settings(),
            transport=httpx.MockTransport(
                lambda request: httpx.Response(404, text='model "nope" not found, try pulling it first')
            ),
        ).complete("s", "u")

        assert result.status == STATUS_MODEL_MISSING
        assert "not installed" in result.reason

    def test_missing_model_reported_from_a_400_body(self):
        result = OllamaProvider(
            llm_settings(),
            transport=httpx.MockTransport(lambda request: httpx.Response(400, text="model nope not found")),
        ).complete("s", "u")

        assert result.status == STATUS_MODEL_MISSING

    def test_server_error_does_not_raise(self):
        result = OllamaProvider(
            llm_settings(), transport=httpx.MockTransport(lambda request: httpx.Response(500, text="boom"))
        ).complete("s", "u")

        assert result.status == "error"
        assert result.ok is False

    @pytest.mark.parametrize(
        "body",
        [
            {"message": {"content": "this is not json"}},
            {"message": {"content": "[1, 2, 3]"}},
            {"message": {"content": "{}"}},
            {"message": {"content": "null"}},
            {"unexpected": "shape"},
        ],
        ids=["not-json", "array", "empty-object", "null", "missing-content"],
    )
    def test_malformed_output_is_reported(self, body):
        result = OllamaProvider(
            llm_settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body))
        ).complete("s", "u")

        assert result.status == STATUS_MALFORMED
        assert result.payload == {}

    def test_non_json_http_response_is_malformed(self):
        result = OllamaProvider(
            llm_settings(), transport=httpx.MockTransport(lambda request: httpx.Response(200, text="<html>nope</html>"))
        ).complete("s", "u")

        assert result.status == STATUS_MALFORMED

    def test_unconfigured_model_does_not_call_anything(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"message": {"content": "{}"}})

        result = OllamaProvider(llm_settings(model=""), transport=httpx.MockTransport(handler)).complete("s", "u")

        assert result.status == STATUS_NOT_CONFIGURED
        assert calls == []

    def test_disabled_provider_does_not_call_anything(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"message": {"content": "{}"}})

        result = OllamaProvider(llm_settings(enabled=False), transport=httpx.MockTransport(handler)).complete("s", "u")

        assert result.status == "disabled"
        assert calls == []

    def test_drops_unexpected_fields_and_wrong_types(self):
        noisy = {
            "headline": "  spaced   out  ",
            "explanation": 12345,
            "suggestions": ["good one", 42, None, "second"],
            "caveats": "not a list",
            "system_prompt": "attempted prompt leak",
            "role": "developer",
        }
        result = OllamaProvider(
            llm_settings(), transport=httpx.MockTransport(reply_with(noisy))
        ).complete("s", "u")

        assert result.status == STATUS_OK
        assert result.payload == {
            "headline": "spaced out",
            "explanation": "",
            "suggestions": ["good one", "second"],
            "caveats": [],
        }

    def test_describe_reports_no_credentials(self):
        detail = OllamaProvider(llm_settings()).describe()

        assert detail["requires_api_key"] is False
        assert "api_key" not in detail
        assert detail["model"] == MODEL


class TestOllamaProbe:
    """The probe runs while the dashboard loads, so it must be cheap and never raise."""

    def test_reports_ready_when_the_model_is_installed(self):
        provider = OllamaProvider(llm_settings(model="qwen2.5:7b"), transport=httpx.MockTransport(tags_with("qwen2.5:7b")))

        assert provider.probe().status == STATUS_OK

    def test_a_bare_name_matches_the_latest_tag(self):
        provider = OllamaProvider(llm_settings(model="qwen2.5"), transport=httpx.MockTransport(tags_with("qwen2.5:latest")))

        assert provider.probe().status == STATUS_OK

    def test_reports_a_missing_model(self):
        provider = OllamaProvider(llm_settings(model="ghost:1b"), transport=httpx.MockTransport(tags_with("qwen2.5:7b")))

        result = provider.probe()
        assert result.status == STATUS_MODEL_MISSING
        assert "not installed" in result.reason

    def test_reports_unreachable_without_raising(self):
        def handler(request):
            raise httpx.ConnectError("refused")

        assert OllamaProvider(llm_settings(), transport=httpx.MockTransport(handler)).probe().status == STATUS_UNREACHABLE

    def test_reports_timeout_without_raising(self):
        def handler(request):
            raise httpx.ReadTimeout("slow")

        assert OllamaProvider(llm_settings(), transport=httpx.MockTransport(handler)).probe().status == STATUS_TIMEOUT

    def test_probe_does_not_run_inference(self):
        seen = {}

        def handler(request):
            seen["method"] = request.method
            seen["path"] = request.url.path
            return httpx.Response(200, json={"models": [{"name": f"{MODEL}:latest"}]})

        OllamaProvider(llm_settings(), transport=httpx.MockTransport(handler)).probe()

        assert seen == {"method": "GET", "path": "/api/tags"}

    def test_malformed_probe_response(self):
        provider = OllamaProvider(llm_settings(), transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>")))

        assert provider.probe().status == STATUS_MALFORMED

    def test_probe_does_nothing_when_unconfigured(self):
        calls = []

        def handler(request):
            calls.append(request)
            return httpx.Response(200, json={"models": []})

        assert OllamaProvider(llm_settings(model=""), transport=httpx.MockTransport(handler)).probe().status == (
            STATUS_NOT_CONFIGURED
        )
        assert calls == []


class TestProviderContract:
    """Every registered provider must answer a status check without provider-specific code.

    The status endpoint runs on page load, so a provider that forgot to implement ``probe``
    would turn a missing method into a 500 on the dashboard instead of a tidy "unavailable".
    """

    def test_a_provider_with_no_probe_of_its_own_still_answers(self):
        calls: list[str] = []

        class MinimalProvider(LLMProvider):
            """A provider that only knows how to generate, as a first-time author would write it."""

            name = "minimal"

            def complete(self, system_prompt: str, user_prompt: str):
                calls.append("complete")
                return LLMResult(status=STATUS_OK, text="Ready.", provider=self.name, model=self.model)

        result = MinimalProvider(llm_settings()).probe()

        assert result.status == STATUS_OK
        assert calls == ["complete"]

    def test_the_default_probe_reports_disabled_before_calling_the_model(self):
        calls: list[str] = []

        class MinimalProvider(LLMProvider):
            name = "minimal"

            def complete(self, system_prompt: str, user_prompt: str):
                calls.append("complete")
                return LLMResult(status=STATUS_OK)

        assert MinimalProvider(llm_settings(enabled=False)).probe().status == STATUS_DISABLED
        assert MinimalProvider(llm_settings(model="")).probe().status == STATUS_NOT_CONFIGURED
        assert calls == [], "a misconfigured provider must not be called at all"

    def test_every_registered_provider_can_be_probed(self):
        """No registered provider may be missing the status check the dashboard depends on."""
        import inspect

        for name, provider_class in PROVIDERS.items():
            assert callable(getattr(provider_class, "probe", None)), f"{name} cannot be probed"
            # It must be the provider's own method or the portable base default, never missing.
            assert issubclass(provider_class, LLMProvider), f"{name} does not honour the provider contract"

            # Probe without touching a real server: inject a transport where the provider
            # accepts one, so this test can never hang waiting on a local model service.
            kwargs = {}
            if "transport" in inspect.signature(provider_class.__init__).parameters:
                def handler(request):
                    raise httpx.ConnectError("no local model service in this test")

                kwargs["transport"] = httpx.MockTransport(handler)

            result = provider_class(llm_settings(model="probe-me"), **kwargs).probe()
            assert isinstance(result, LLMResult), f"{name} did not return a result"
            assert result.status, f"{name} returned an empty status"
            assert provider_class(llm_settings(), **kwargs).describe()["provider"] == name


# ---------------------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------------------


class TestGrounding:
    @pytest.fixture()
    def context(self):
        return {
            "usage": {
                "days_analysed": 25,
                "total_energy_kwh": 258.0,
                "average_kwh_per_day": 10.32,
            },
            "load_bands": [{"label": "Low draw", "energy_kwh": 31.5, "energy_share_percent": 12.2}],
            "forecast": {"status": "ok", "predicted_avg_kwh_per_day": 10.32, "horizon_days": 7},
            "anomalies": {"count": 0, "findings": []},
            "calculated_suggestions": [
                {
                    "title": "A baseline load never switches off",
                    "potential_saving_kwh_per_month": 32.4,
                    "potential_saving_cost_per_month": 9.07,
                }
            ],
            "projected_bill": {"total_cost": 86.69, "energy_kwh": 309.6},
            "currency": "USD",
        }

    @pytest.mark.parametrize(
        "text",
        [
            "Over 25 days you used 258 kWh, about 10.32 kWh a day.",
            "Low draw accounts for 12.2% of your energy.",
            "You could save 32.4 kWh a month, about 9.07 USD.",
            "The projected bill is 86.69 USD for 309.6 kWh.",
            "1. Shift laundry to daytime\n2. Run the dishwasher earlier",
            "Try a few things across the week rather than all at once.",
            "The forecast is about 10.32 kWh per day for 7 days.",
        ],
    )
    def test_accepts_figures_that_are_in_the_data(self, context, text):
        assert find_ungrounded_measurements(text, context) == []

    @pytest.mark.parametrize(
        "text,expected_fragment",
        [
            ("You used 412.9 kWh in the window.", "412.9"),
            ("The projected bill comes to $214.55.", "214.55"),
            ("That is 37% above your average.", "37%"),
            ("Peak draw hit 3400 W overnight.", "3400 W"),
            ("Around 9,923 kWh was recorded.", "9,923"),
            ("That is 1,234 kWh per month.", "1,234"),
        ],
    )
    def test_catches_figures_that_are_not_in_the_data(self, context, text, expected_fragment):
        found = find_ungrounded_measurements(text, context)
        assert found, f"expected {expected_fragment!r} to be caught"
        assert expected_fragment.split()[0].rstrip(".") in found[0]

    def test_bare_counting_words_are_not_treated_as_measurements(self, context):
        assert find_ungrounded_measurements("Two suggestions and three things to check.", context) == []


# ---------------------------------------------------------------------------------------
# Context distillation
# ---------------------------------------------------------------------------------------


class TestContext:
    def test_context_excludes_device_identity(self):
        summary = {
            "device": {"id": "sim-main-01", "name": "Main meter", "location": "Kitchen"},
            "tariff": {"name": "T", "rate_type": "flat", "currency": "USD", "flat_rate_per_kwh": 0.3},
            "energy_today": {"label": "Metered", "energy_kwh": 1.0},
            "energy_this_period": {"energy_kwh": 10.0},
            "projected_bill": {"total": 3.0, "energy": {"total_kwh": 10.0}},
        }
        context = build_context(
            summary=summary,
            insights={"days_analysed": 5, "total_energy_kwh": 10.0, "insights": [], "load_bands": []},
            forecast={"status": "insufficient_history"},
            anomalies={"status": "ok", "findings": []},
            data_label="Metered",
        )

        blob = json.dumps(context)
        assert "sim-main-01" not in blob
        assert "Main meter" not in blob
        assert "Kitchen" not in blob
        assert context["scope"] == "the metered site"
        # The bill must actually reach the context, not a block of nulls from reading the
        # wrong key. The payload calls the grand total "total" and nests energy under "energy".
        assert context["projected_bill"]["total_cost"] == 3.0
        assert context["projected_bill"]["energy_kwh"] == 10.0

    def test_the_bill_context_is_never_silently_empty(self):
        """A bill payload with real figures must not arrive at the model as all nulls."""
        context = build_context(
            summary={
                "tariff": {"name": "T", "rate_type": "flat", "currency": "INR", "flat_rate_per_kwh": 5.0},
                "energy_today": {},
                "energy_this_period": {},
                "projected_bill": {
                    "total": 1180.44,
                    "energy_charge": 1052.8,
                    "standing_charge": 75.0,
                    "duty_label": "Electricity duty",
                    "tax": 52.64,
                    "energy": {"total_kwh": 348.4},
                    "projection_basis": "metered plus forecast",
                    "is_estimate": True,
                },
            },
            insights={"days_analysed": 26, "total_energy_kwh": 309.5},
            forecast={"status": "insufficient_history"},
            anomalies={"status": "ok", "findings": []},
            data_label="Metered",
        )
        bill = context["projected_bill"]
        assert bill["total_cost"] == 1180.44
        assert bill["energy_kwh"] == 348.4
        assert bill["energy_charge"] == 1052.8
        assert bill["fixed_charge"] == 75.0
        assert bill["duty"] == 52.64
        assert bill["duty_label"] == "Electricity duty"
        assert bill["basis"] == "metered plus forecast"

    def test_a_slab_tariff_never_reports_a_flat_rate_of_zero(self):
        context = build_context(
            summary={
                "tariff": {
                    "name": "Generic India domestic (example slab tariff)",
                    "rate_type": "slab",
                    "currency": "INR",
                    "flat_rate_per_kwh": None,
                },
                "energy_today": {"energy_kwh": 12.0},
                "energy_this_period": {"energy_kwh": 309.5, "cost": 1211.75},
                "projected_bill": {"total": 1610.1, "energy": {"total_kwh": 348.0}},
            },
            insights={"days_analysed": 26, "total_energy_kwh": 309.5},
            forecast={"status": "insufficient_history"},
            anomalies={"status": "ok", "findings": []},
            data_label="Metered",
        )
        tariff = context["tariff"]
        assert tariff["rate_type"] == "slab"
        # Not 0.0, which is what reading the (absent) flat rate used to produce.
        assert tariff["effective_rate_per_kwh"] == pytest.approx(1211.75 / 309.5, abs=0.01)
        assert "not a fixed price" in tariff["rate_note"]

    def test_the_system_prompt_forbids_naming_devices_and_claiming_a_fixed_slab_rate(self):
        from energy_ai.llm.prompt import SYSTEM_PROMPT

        assert "not even as an example" in SYSTEM_PROMPT
        assert "rate_note" in SYSTEM_PROMPT

    def test_context_caps_long_lists(self):
        insights = {
            "days_analysed": 30,
            "total_energy_kwh": 100.0,
            "load_bands": [{"label": f"b{i}", "energy_kwh": 1.0} for i in range(20)],
            "insights": [
                {"id": f"i{i}", "title": f"t{i}", "summary": "s", "evidence": [f"e{j}" for j in range(20)]}
                for i in range(20)
            ],
        }
        context = build_context(
            summary={"tariff": {}, "energy_today": {}, "energy_this_period": {}, "projected_bill": {}},
            insights=insights,
            forecast={"status": "ok", "points": [{"date": f"2026-09-{d:02d}", "energy_kwh": 1.0, "kind": "forecast"} for d in range(1, 30)]},
            anomalies={"status": "ok", "findings": [{"title": f"f{i}"} for i in range(20)]},
            data_label="Metered",
        )

        assert len(context["calculated_suggestions"]) == 5
        assert len(context["load_bands"]) == 5
        assert len(context["forecast"]["next_days"]) == 14
        assert len(context["anomalies"]["findings"]) == 5
        assert len(context["calculated_suggestions"][0]["evidence"]) == 3

    def test_actual_forecast_history_is_not_sent_as_forecast(self):
        context = build_context(
            summary={"tariff": {}, "energy_today": {}, "energy_this_period": {}, "projected_bill": {}},
            insights={"days_analysed": 3, "total_energy_kwh": 6.0},
            forecast={
                "status": "ok",
                "points": [
                    {"date": "2026-09-01", "energy_kwh": 2.0, "kind": "actual"},
                    {"date": "2026-09-26", "energy_kwh": 2.0, "kind": "forecast"},
                ],
            },
            anomalies={"status": "ok", "findings": []},
            data_label="Metered",
        )

        assert [p["date"] for p in context["forecast"]["next_days"]] == ["2026-09-26"]

    def test_prompt_contains_the_data_and_no_instructions(self):
        context = {"usage": {"total_energy_kwh": 5.0}}
        prompt = build_user_prompt(context)

        assert "total_energy_kwh" in prompt
        assert "<data>" in prompt and "</data>" in prompt


class TestFallbackNarrative:
    def test_uses_only_calculated_values(self):
        narrative = fallback_narrative(
            {
                "usage": {"days_analysed": 30, "total_energy_kwh": 214.5, "average_kwh_per_day": 7.15},
                "forecast": {"status": "ok", "horizon_days": 7, "predicted_avg_kwh_per_day": 7.4, "confidence": "moderate"},
                "anomalies": {"count": 2},
                "calculated_suggestions": [{"title": "Shift flexible loads"}],
                "currency": "USD",
            }
        )

        assert "214.5" in narrative["explanation"]
        assert "Shift flexible loads" in narrative["suggestions"]
        assert any("not by a language model" in caveat for caveat in narrative["caveats"])
        assert find_ungrounded_measurements(narrative["explanation"], {
            "usage": {"days_analysed": 30, "total_energy_kwh": 214.5, "average_kwh_per_day": 7.15},
            "forecast": {"horizon_days": 7, "predicted_avg_kwh_per_day": 7.4},
        }) == []

    def test_copes_with_no_calculations(self):
        narrative = fallback_narrative({})

        assert narrative["explanation"]
        assert narrative["suggestions"]


# ---------------------------------------------------------------------------------------
# Endpoint behaviour
# ---------------------------------------------------------------------------------------


class TestStatusEndpoint:
    def test_reports_unavailable_when_no_model_is_set(self, client, monkeypatch):
        monkeypatch.setattr(llm_insights, "get_settings", lambda: _SettingsShim(llm_settings(model="")))
        body = client.get("/api/ai/status").json()

        assert body["ai_available"] is False
        assert body["status"] == STATUS_NOT_CONFIGURED
        assert body["model"] is None
        assert body["message"]

    def test_reports_ready_when_a_model_is_configured(self, client, monkeypatch):
        install_model(
            monkeypatch,
            lambda request: httpx.Response(200, json={"models": [{"name": f"{MODEL}:latest"}]}),
        )
        body = client.get("/api/ai/status").json()

        assert body["ai_available"] is True
        assert body["configured"] is True
        assert body["model"] == MODEL
        assert body["provider"] == "ollama"

    def test_configured_but_unreachable_is_not_reported_as_ready(self, client, monkeypatch):
        install_model(monkeypatch, lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")))
        body = client.get("/api/ai/status").json()

        assert body["ai_available"] is False
        assert body["configured"] is True
        assert body["status"] == STATUS_UNREACHABLE
        assert body["message"]

    def test_configured_but_model_not_pulled(self, client, monkeypatch):
        install_model(monkeypatch, lambda request: httpx.Response(200, json={"models": [{"name": "other:7b"}]}))
        body = client.get("/api/ai/status").json()

        assert body["ai_available"] is False
        assert body["configured"] is True
        assert body["status"] == STATUS_MODEL_MISSING

    def test_bare_model_name_matches_a_latest_tag(self, client, monkeypatch):
        install_model(
            monkeypatch,
            lambda request: httpx.Response(200, json={"models": [{"name": "some-local-model:latest"}]}),
        )
        assert client.get("/api/ai/status").json()["ai_available"] is True

    def test_reports_disabled_without_error(self, client, monkeypatch):
        monkeypatch.setattr(llm_insights, "get_settings", lambda: _SettingsShim(llm_settings(enabled=False)))
        body = client.get("/api/ai/status").json()

        assert body["ai_available"] is False
        assert body["status"] == "disabled"

    def test_unknown_provider_is_reported_not_raised(self, client, monkeypatch):
        monkeypatch.setattr(llm_insights, "get_settings", lambda: _SettingsShim(llm_settings(provider="nope")))
        body = client.get("/api/ai/status").json()

        assert body["ai_available"] is False
        assert "nope" in body["message"]

    def test_status_needs_no_data(self, client):
        assert client.get("/api/ai/status").status_code == 200


class TestExplainEndpoint:
    def test_without_any_readings(self, client):
        response = client.post("/api/ai/explain", json={})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "no_data"
        assert body["ai_available"] is False
        assert body["generated"]["suggestions"] == []

    def test_works_with_no_model_configured(self, client, monkeypatch):
        seed_history(client)
        monkeypatch.setattr(llm_insights, "get_settings", lambda: _SettingsShim(llm_settings(model="")))

        body = client.post("/api/ai/explain", json={}).json()

        assert body["ai_available"] is False
        assert body["status"] == STATUS_NOT_CONFIGURED
        assert body["source"] == "calculated"
        assert body["generated"]["explanation"]
        assert body["calculated"]["usage"]["total_energy_kwh"] > 0

    def test_uses_the_model_when_it_answers(self, client, monkeypatch):
        seed_history(client)
        install_model(monkeypatch, reply_with(model_reply()))

        body = client.post("/api/ai/explain", json={}).json()

        assert body["status"] == STATUS_OK
        assert body["ai_available"] is True
        assert body["source"] == "model"
        assert body["model"] == MODEL
        assert body["generated"]["headline"]
        assert body["generated"]["suggestions"]
        # The calculated block is returned separately, untouched by the model.
        assert body["calculated"]["usage"]["total_energy_kwh"] > 0

    def test_generated_text_and_calculated_values_are_separate_blocks(self, client, monkeypatch):
        seed_history(client)
        install_model(monkeypatch, reply_with(model_reply()))

        body = client.post("/api/ai/explain", json={}).json()

        assert set(body) >= {"generated", "calculated", "source"}
        # Nothing numeric in the model's prose is promoted into the calculated block.
        assert isinstance(body["calculated"]["usage"]["total_energy_kwh"], float)
        assert body["generated"].keys() == {"headline", "explanation", "suggestions", "caveats"}

    def test_discards_model_text_that_invents_a_figure(self, client, monkeypatch):
        seed_history(client)
        install_model(
            monkeypatch,
            reply_with(model_reply(explanation="You used 4129.5 kWh this period, a record.")),
        )

        body = client.post("/api/ai/explain", json={}).json()

        assert body["status"] == STATUS_UNGROUNDED
        assert body["ai_available"] is True
        assert body["source"] == "calculated"
        assert "4129.5" not in json.dumps(body["generated"])
        assert body["warnings"]
        assert "discarded" in body["warnings"][0]

    @pytest.mark.parametrize(
        "handler,expected",
        [
            (lambda request: (_ for _ in ()).throw(httpx.ConnectError("refused")), STATUS_UNREACHABLE),
            (lambda request: (_ for _ in ()).throw(httpx.ReadTimeout("slow")), STATUS_TIMEOUT),
            (lambda request: httpx.Response(404, text="model not found"), STATUS_MODEL_MISSING),
            (lambda request: httpx.Response(200, text="not json"), STATUS_MALFORMED),
        ],
        ids=["stopped", "slow", "model-missing", "malformed"],
    )
    def test_every_failure_still_returns_usable_output(self, client, monkeypatch, handler, expected):
        seed_history(client)
        install_model(monkeypatch, handler)

        response = client.post("/api/ai/explain", json={})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == expected
        assert body["ai_available"] is False
        assert body["source"] == "calculated"
        assert body["message"]
        assert body["generated"]["explanation"]
        assert body["calculated"]["usage"]["total_energy_kwh"] > 0

    def test_provider_crash_does_not_propagate(self):
        # A provider that raises must still come back as a normal failure result, so a bug in a
        # provider can never surface as a 500 on the dashboard.
        class Boom:
            name = "boom"
            model = MODEL

            def complete(self, system_prompt, user_prompt):
                raise RuntimeError("provider bug")

        result = llm_insights._generate(Boom(), {})

        assert result.ok is False
        assert result.status == "error"
        assert result.reason

    def test_device_id_is_not_forwarded_to_the_model(self, client, monkeypatch):
        seed_history(client, device_id="sim-main-01")
        seen = {}

        def handler(request):
            seen["body"] = json.loads(request.content)
            return httpx.Response(200, json={"message": {"content": json.dumps(model_reply())}})

        install_model(monkeypatch, handler)
        client.post("/api/ai/explain", json={"device_id": "sim-main-01"})

        prompt = seen["body"]["messages"][1]["content"]
        assert "sim-main-01" not in prompt

    def test_accepts_an_empty_body(self, client, monkeypatch):
        seed_history(client)
        install_model(monkeypatch, reply_with(model_reply()))

        assert client.post("/api/ai/explain").status_code == 200

    def test_horizon_is_validated(self, client):
        assert client.post("/api/ai/explain", json={"horizon_days": 0}).status_code == 422
        assert client.post("/api/ai/explain", json={"horizon_days": 999}).status_code == 422

    def test_core_dashboard_is_unaffected_with_no_model(self, client, monkeypatch):
        seed_history(client)
        monkeypatch.setattr(llm_insights, "get_settings", lambda: _SettingsShim(llm_settings(model="")))

        for path in ("/api/analytics/summary", "/api/ai/forecast", "/api/ai/anomalies", "/api/ai/insights", "/api/billing/tariff"):
            assert client.get(path).status_code == 200, path

        assert client.post("/api/ai/explain", json={}).status_code == 200
