"""Tests for the OrcaRouter Cyber Inference provider and model catalog."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import openai
import pytest

try:  # the OpenAI SDK ships one of these HTTP transports depending on the release
    import httpx2 as httpx_client
except ImportError:  # pragma: no cover
    import httpx as httpx_client

from re_agent.cli.main import main
from re_agent.config.schema import LLMConfig
from re_agent.llm import orcarouter
from re_agent.llm.orcarouter import (
    CATALOG_MAX_BYTES,
    ORCAROUTER_DEFAULT_API_BASE,
    ORCAROUTER_DEFAULT_AUTH_BASE,
    ORCAROUTER_DEFAULT_MODEL,
    ORCAROUTER_DISPLAY_NAME,
    ORCAROUTER_KEY_ENV,
    ORCAROUTER_KEY_URL,
    OrcaModel,
    OrcaRouterAuthError,
    OrcaRouterCatalogError,
    OrcaRouterProvider,
    OrcaRouterRateLimitError,
    discover_models,
    filter_models,
    parse_catalog,
    resolve_api_base,
    resolve_api_key,
    resolve_auth_base,
)
from re_agent.llm.protocol import LLMProvider, Message
from re_agent.llm.registry import create_provider

TEST_KEY = "sk-orca-test-key-not-real"


def _catalog_payload() -> dict[str, Any]:
    return {
        "object": "list",
        "data": [
            {
                "id": "vendor/text-chat",
                "supported_endpoint_types": ["openai", "openai-response"],
                "context_length": 128000,
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
            },
            {
                "id": "vendor/vision-chat",
                "supported_endpoint_types": ["openai", "anthropic"],
                "context_length": 200000,
                "architecture": {"input_modalities": ["text", "image"], "output_modalities": ["text"]},
            },
            {
                "id": "vendor/named-vision-but-undeclared",
                "supported_endpoint_types": ["openai"],
            },
            {
                "id": "vendor/embed",
                "supported_endpoint_types": ["embeddings"],
                "architecture": {"input_modalities": ["text"], "output_modalities": ["embedding"]},
            },
            {
                "id": "vendor/image-gen",
                "supported_endpoint_types": ["image-generation"],
                "architecture": {"input_modalities": ["text"], "output_modalities": ["image"]},
            },
            {
                "id": "vendor/video-gen",
                "supported_endpoint_types": ["openai-video"],
                "architecture": {"input_modalities": ["text"], "output_modalities": ["video"]},
            },
            {
                "id": "vendor/reranker",
                "supported_endpoint_types": ["jina-rerank"],
            },
            {
                "id": "vendor/reasoning-chat",
                "supported_endpoint_types": ["anthropic"],
                "context_length": 400000,
                "architecture": {"input_modalities": ["text"], "output_modalities": ["text"]},
                "reasoning": {"efforts": ["low", "medium", "high"]},
            },
            {"id": "not-namespaced", "supported_endpoint_types": ["openai"]},
            "not-a-record",
        ],
    }


def _models() -> tuple[OrcaModel, ...]:
    return parse_catalog(_catalog_payload())


def _clear_origin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        ORCAROUTER_KEY_ENV,
        "ORCA_API_BASE_URL",
        "ORCA_AUTH_BASE_URL",
        "ORCA_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


def _fake_response(content: str = "ready") -> Any:
    message = SimpleNamespace(content=content)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=None)


class _DummyCompletions:
    def create(self, **kwargs: Any) -> Any:
        return _fake_response()


class _DummyChat:
    def __init__(self) -> None:
        self.completions = _DummyCompletions()


class _DummyClient:
    def __init__(self, **kwargs: Any) -> None:
        self.kwargs = kwargs
        self.base_url = kwargs.get("base_url")
        self.chat = _DummyChat()


def _fake_openai(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    created: dict[str, Any] = {}

    def factory(**kwargs: Any) -> _DummyClient:
        client = _DummyClient(**kwargs)
        created.update(kwargs)
        created["client"] = client
        return client

    monkeypatch.setattr(openai, "OpenAI", factory)
    return created


def _provider(monkeypatch: pytest.MonkeyPatch, **kwargs: Any) -> OrcaRouterProvider:
    _clear_origin_env(monkeypatch)
    _fake_openai(monkeypatch)
    return OrcaRouterProvider(api_key=TEST_KEY, **kwargs)


def test_registry_creates_named_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)
    created = _fake_openai(monkeypatch)
    provider = create_provider(LLMConfig(provider="orcarouter", api_key=TEST_KEY))
    assert isinstance(provider, OrcaRouterProvider)
    assert isinstance(provider, LLMProvider)
    assert provider.supports_conversations
    assert provider._model == ORCAROUTER_DEFAULT_MODEL
    assert provider.provider_name == ORCAROUTER_DISPLAY_NAME
    assert created["api_key"] == TEST_KEY
    assert created["base_url"] == ORCAROUTER_DEFAULT_API_BASE
    assert provider.base_url == ORCAROUTER_DEFAULT_API_BASE


def test_registry_keeps_configured_model_and_base(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)
    created = _fake_openai(monkeypatch)
    provider = create_provider(
        LLMConfig(
            provider="orcarouter",
            api_key=TEST_KEY,
            model="orca/orcacyber-ultra-1.0",
            base_url="https://gateway.internal.example/v1",
        )
    )
    assert provider._model == "orca/orcacyber-ultra-1.0"
    assert created["base_url"] == "https://gateway.internal.example/v1"


def test_api_key_uses_config_then_named_env(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)
    assert resolve_api_key(TEST_KEY) == TEST_KEY
    monkeypatch.setenv(ORCAROUTER_KEY_ENV, TEST_KEY)
    assert resolve_api_key() == TEST_KEY
    assert resolve_api_key("sk-orca-config-wins") == "sk-orca-config-wins"


def test_api_key_error_is_actionable_and_never_echoes_a_key(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-openai-must-not-be-used")
    with pytest.raises(OrcaRouterAuthError) as info:
        resolve_api_key(None)
    message = str(info.value)
    assert ORCAROUTER_KEY_URL in message
    assert ORCAROUTER_KEY_ENV in message
    assert "sk-openai" not in message


def test_origin_resolution_precedence(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)
    assert resolve_api_base() == ORCAROUTER_DEFAULT_API_BASE
    assert resolve_auth_base() == ORCAROUTER_DEFAULT_AUTH_BASE
    assert resolve_api_base("https://gateway.example/v1") == "https://gateway.example/v1"
    monkeypatch.setenv("ORCA_API_BASE_URL", "https://api.self.example/v1")
    assert resolve_api_base() == "https://api.self.example/v1"
    monkeypatch.setenv("ORCA_AUTH_BASE_URL", "https://auth.self.example")
    assert resolve_auth_base() == "https://auth.self.example"
    assert resolve_api_base() == "https://api.self.example/v1"


def test_shared_self_hosted_base_adds_v1_for_inference_only(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)
    monkeypatch.setenv("ORCA_BASE_URL", "https://orcarouter.self.example")
    assert resolve_auth_base() == "https://orcarouter.self.example"
    assert resolve_api_base() == "https://orcarouter.self.example/v1"


def test_origin_policy_requires_https_except_loopback(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)
    with pytest.raises(ValueError):
        resolve_api_base("http://gateway.example/v1")
    with pytest.raises(ValueError):
        resolve_api_base("https://user:pass@gateway.example/v1")
    assert resolve_api_base("http://127.0.0.1:8080/v1") == "http://127.0.0.1:8080/v1"
    assert resolve_api_base("http://localhost:8080/v1") == "http://localhost:8080/v1"


def test_parse_catalog_preserves_namespace_and_metadata() -> None:
    models = _models()
    by_id = {model.id: model for model in models}
    assert "not-namespaced" not in by_id
    vision = by_id["vendor/vision-chat"]
    assert vision.context_length == 200000
    assert vision.input_modalities == ("text", "image")
    assert vision.supported_endpoint_types == ("openai", "anthropic")
    assert vision.source == "live"
    assert by_id["vendor/reasoning-chat"].reasoning_efforts == ("low", "medium", "high")


def test_capability_filters_are_fail_closed() -> None:
    models = _models()
    chat_ids = {model.id for model in filter_models(models, "chat")}
    assert {"vendor/text-chat", "vendor/vision-chat", "vendor/reasoning-chat"} <= chat_ids
    assert not chat_ids & {"vendor/embed", "vendor/image-gen", "vendor/video-gen", "vendor/reranker"}

    image_chat_ids = {model.id for model in filter_models(models, "chat-image")}
    assert image_chat_ids == {"vendor/vision-chat"}
    assert "vendor/named-vision-but-undeclared" not in image_chat_ids

    assert {model.id for model in filter_models(models, "embedding")} == {"vendor/embed"}
    assert {model.id for model in filter_models(models, "image")} == {"vendor/image-gen"}
    assert {model.id for model in filter_models(models, "video")} == {"vendor/video-gen"}
    assert {model.id for model in filter_models(models, "rerank")} == {"vendor/reranker"}
    assert len(filter_models(models, "all")) == len(models)
    with pytest.raises(ValueError):
        filter_models(models, "telepathy")


def test_discovery_uses_live_result_as_authoritative(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)
    calls: list[tuple[str, str, float]] = []
    live = (OrcaModel("vendor/only-live", "Only Live", 1000, ("text",), ("text",), ("openai",), (), "live"),)

    def fetch(url: str, key: str, timeout: float) -> tuple[OrcaModel, ...]:
        calls.append((url, key, timeout))
        return live

    catalog = discover_models(api_key=TEST_KEY, capability="chat", fetcher=fetch)
    assert catalog.source == "live"
    assert catalog.degraded is False
    assert catalog.error is None
    assert catalog.models == live
    assert len(catalog.models) == 1
    assert calls[0][0] == "https://api.orcarouter.ai/v1/models?capability=chat"
    assert calls[0][1] == TEST_KEY


def test_discovery_falls_back_to_verified_seed_with_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)

    def fetch(url: str, key: str, timeout: float) -> tuple[OrcaModel, ...]:
        raise OrcaRouterCatalogError("Could not reach the OrcaRouter model catalog (network error).")

    catalog = discover_models(api_key=TEST_KEY, capability="chat", fetcher=fetch)
    assert catalog.source == "seed"
    assert catalog.degraded is True
    assert catalog.error
    seed_ids = {model.id for model in catalog.models}
    assert {"openai/gpt-5.5", "anthropic/claude-opus-4.8", "google/gemini-3.5-flash",
            "deepseek/deepseek-v4-pro", "orcarouter/auto"} <= seed_ids
    gpt = next(model for model in catalog.models if model.id == "openai/gpt-5.5")
    assert gpt.reasoning_efforts == ("low", "medium", "high", "xhigh")
    assert "image" in gpt.input_modalities
    assert gpt.context_length == 400000
    assert TEST_KEY not in (catalog.error or "")

    image_catalog = discover_models(api_key=TEST_KEY, capability="chat-image", fetcher=fetch)
    image_ids = {model.id for model in image_catalog.models}
    assert "openai/gpt-5.5" in image_ids
    assert "deepseek/deepseek-v4-pro" not in image_ids


def test_discovery_propagates_terminal_credential_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)

    def unauthorized(url: str, key: str, timeout: float) -> tuple[OrcaModel, ...]:
        raise OrcaRouterAuthError("OrcaRouter rejected the API key (HTTP 401).")

    def throttled(url: str, key: str, timeout: float) -> tuple[OrcaModel, ...]:
        raise OrcaRouterRateLimitError("OrcaRouter rate-limited this request (HTTP 429).")

    with pytest.raises(OrcaRouterAuthError):
        discover_models(api_key=TEST_KEY, fetcher=unauthorized)
    with pytest.raises(OrcaRouterRateLimitError):
        discover_models(api_key=TEST_KEY, fetcher=throttled)


class _FakeResponse:
    status = 200

    def __init__(self, body: bytes) -> None:
        self._body = body

    def read(self, size: int = -1) -> bytes:
        return self._body

    def __enter__(self) -> _FakeResponse:
        return self

    def __exit__(self, *exc_info: object) -> bool:
        return False


def test_catalog_transport_maps_http_status(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)

    def raise_http(code: int) -> Any:
        def urlopen(*args: object, **kwargs: object) -> Any:
            raise urllib.error.HTTPError("https://api.orcarouter.ai/v1/models", code, "boom", None, None)

        return urlopen

    monkeypatch.setattr(urllib.request, "urlopen", raise_http(401))
    with pytest.raises(OrcaRouterAuthError):
        orcarouter._fetch_catalog("https://api.orcarouter.ai/v1/models", TEST_KEY, 5.0)
    monkeypatch.setattr(urllib.request, "urlopen", raise_http(429))
    with pytest.raises(OrcaRouterRateLimitError):
        orcarouter._fetch_catalog("https://api.orcarouter.ai/v1/models", TEST_KEY, 5.0)
    monkeypatch.setattr(urllib.request, "urlopen", raise_http(503))
    with pytest.raises(OrcaRouterCatalogError) as info:
        orcarouter._fetch_catalog("https://api.orcarouter.ai/v1/models", TEST_KEY, 5.0)
    assert "503" in str(info.value)
    assert TEST_KEY not in str(info.value)


def test_catalog_transport_bounds_size_and_network_errors(monkeypatch: pytest.MonkeyPatch) -> None:
    _clear_origin_env(monkeypatch)
    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: _FakeResponse(b"x" * (CATALOG_MAX_BYTES + 1)),
    )
    with pytest.raises(OrcaRouterCatalogError) as info:
        orcarouter._fetch_catalog("https://api.orcarouter.ai/v1/models", TEST_KEY, 5.0)
    assert "size limit" in str(info.value)

    def network_error(*args: object, **kwargs: object) -> Any:
        raise urllib.error.URLError("dns failure")

    monkeypatch.setattr(urllib.request, "urlopen", network_error)
    with pytest.raises(OrcaRouterCatalogError) as info:
        orcarouter._fetch_catalog("https://api.orcarouter.ai/v1/models", TEST_KEY, 5.0)
    assert "network error" in str(info.value)
    assert TEST_KEY not in str(info.value)


def test_provider_send_uses_chat_completions_and_records_metadata(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = _provider(monkeypatch)
    captured: dict[str, Any] = {}

    def create(**kwargs: Any) -> Any:
        captured.update(kwargs)
        return _fake_response("ready")

    monkeypatch.setattr(provider._client.chat.completions, "create", create)
    text = provider.send([Message(role="user", content="hello")])
    assert text == "ready"
    assert captured["model"] == ORCAROUTER_DEFAULT_MODEL
    assert provider.last_metadata["provider"] == "orcarouter"
    assert provider.last_metadata["provider_name"] == ORCAROUTER_DISPLAY_NAME
    assert provider.last_metadata["base_url"] == ORCAROUTER_DEFAULT_API_BASE


def _http_error(status: int, error_cls: type[openai.APIStatusError]) -> openai.APIStatusError:
    request = httpx_client.Request("POST", "https://api.orcarouter.ai/v1/chat/completions")
    response = httpx_client.Response(status, request=request)
    return error_cls("upstream failure", response=response, body=None)


@pytest.mark.parametrize(
    ("status", "error_cls", "expected"),
    [
        (401, openai.AuthenticationError, OrcaRouterAuthError),
        (403, openai.PermissionDeniedError, OrcaRouterAuthError),
        (429, openai.RateLimitError, OrcaRouterRateLimitError),
    ],
)
def test_provider_maps_gateway_failures_without_leaking_the_key(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
    error_cls: type[openai.APIStatusError],
    expected: type[Exception],
) -> None:
    provider = _provider(monkeypatch)

    def create(**kwargs: Any) -> Any:
        raise _http_error(status, error_cls)

    monkeypatch.setattr(provider._client.chat.completions, "create", create)
    with pytest.raises(expected) as info:
        provider.send([Message(role="user", content="hello")])
    message = str(info.value)
    assert TEST_KEY not in message
    assert "HTTP" in message or ORCAROUTER_KEY_URL in message


def test_models_cli_json_output_and_degraded_notice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_origin_env(monkeypatch)
    config_path = tmp_path / "re-agent.yaml"
    config_path.write_text(
        f"llm:\n  provider: orcarouter\n  model: orcarouter/auto\n  api_key: {TEST_KEY}\n",
        encoding="utf-8",
    )

    def live_fetch(url: str, key: str, timeout: float) -> tuple[OrcaModel, ...]:
        return _models()

    monkeypatch.setattr(orcarouter, "_fetch_catalog", live_fetch)
    assert main(["--config", str(config_path), "models", "--capability", "chat", "--json"]) == 0
    live_output = json.loads(capsys.readouterr().out)
    assert live_output["display_name"] == ORCAROUTER_DISPLAY_NAME
    assert live_output["source"] == "live"
    live_ids = {model["id"] for model in live_output["models"]}
    assert "vendor/text-chat" in live_ids
    assert "vendor/embed" not in live_ids

    def failing_fetch(url: str, key: str, timeout: float) -> tuple[OrcaModel, ...]:
        raise OrcaRouterCatalogError("Could not reach the OrcaRouter model catalog (network error).")

    monkeypatch.setattr(orcarouter, "_fetch_catalog", failing_fetch)
    assert main(["--config", str(config_path), "models", "--capability", "chat"]) == 0
    captured = capsys.readouterr()
    assert "openai/gpt-5.5" in captured.out
    assert "source=seed" in captured.out
    assert "verified fallback seed" in captured.err


def test_models_cli_uses_role_specific_orcarouter_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_origin_env(monkeypatch)
    config_path = tmp_path / "re-agent.yaml"
    config_path.write_text(
        "llm:\n  provider: claude\n"
        "agents:\n  reverser:\n    provider: orcarouter\n    model: orcarouter/auto\n"
        f"    api_key: {TEST_KEY}\n    base_url: https://gateway.internal.example/v1\n",
        encoding="utf-8",
    )
    captured: dict[str, str] = {}

    def fetch(url: str, key: str, timeout: float) -> tuple[OrcaModel, ...]:
        captured.update(url=url, key=key)
        return _models()

    monkeypatch.setattr(orcarouter, "_fetch_catalog", fetch)
    assert main(["--config", str(config_path), "models", "--json"]) == 0
    assert captured["url"].startswith("https://gateway.internal.example/v1/models")
    assert captured["key"] == TEST_KEY


def test_models_cli_requires_a_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_origin_env(monkeypatch)
    config_path = tmp_path / "re-agent.yaml"
    config_path.write_text("llm:\n  provider: orcarouter\n  model: orcarouter/auto\n", encoding="utf-8")
    assert main(["--config", str(config_path), "models"]) == 1
    captured = capsys.readouterr()
    assert ORCAROUTER_KEY_URL in captured.err
    assert "sk-orca" not in captured.err


def test_doctor_reports_missing_orcarouter_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    _clear_origin_env(monkeypatch)
    config_path = tmp_path / "re-agent.yaml"
    config_path.write_text(
        "llm:\n  provider: orcarouter\n  model: orcarouter/auto\nproject_profile:\n  source_root: .\n",
        encoding="utf-8",
    )
    main(["--config", str(config_path), "doctor"])
    payload = json.loads(capsys.readouterr().out)
    checks = {check["check"]: check for check in payload["checks"]}
    assert checks["reverser OrcaRouter key"]["passed"] is False
    assert ORCAROUTER_KEY_URL in checks["reverser OrcaRouter key"]["detail"]
