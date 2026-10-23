"""OrcaRouter Cyber Inference provider and model catalog.

OrcaRouter is an OpenAI-compatible gateway for security teams. Inference and
model discovery live on ``https://api.orcarouter.ai/v1`` while account and key
management live on ``https://www.orcarouter.ai``. This module keeps the two
public origins explicit instead of deriving one from the other.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

import openai

from re_agent.llm.openai_compat import OpenAIProvider
from re_agent.llm.protocol import Message

ORCAROUTER_PROVIDER = "orcarouter"
ORCAROUTER_DISPLAY_NAME = "OrcaRouter Cyber Inference"
ORCAROUTER_DEFAULT_API_BASE = "https://api.orcarouter.ai/v1"
ORCAROUTER_DEFAULT_AUTH_BASE = "https://www.orcarouter.ai"
ORCAROUTER_KEY_URL = "https://www.orcarouter.ai/console"
ORCAROUTER_CYBER_URL = "https://www.orcarouter.ai/cyber"
ORCAROUTER_CYBER_MODELS_URL = "https://www.orcarouter.ai/models?tier=cyber"
ORCAROUTER_DEFAULT_MODEL = "orcarouter/auto"
ORCAROUTER_FALLBACK_MODEL = "orcarouter/fusion"

ORCAROUTER_KEY_ENV = "ORCAROUTER_API_KEY"
ORCA_API_BASE_ENV = "ORCA_API_BASE_URL"
ORCA_AUTH_BASE_ENV = "ORCA_AUTH_BASE_URL"
ORCA_BASE_ENV = "ORCA_BASE_URL"

CATALOG_TIMEOUT_S = 10.0
CATALOG_MAX_BYTES = 2_000_000
CATALOG_MAX_MODELS = 2_000

CHAT_ENDPOINT_TYPES = frozenset({"openai", "anthropic", "gemini", "openai-response"})
EMBEDDING_ENDPOINT_TYPES = frozenset({"embeddings"})
IMAGE_ENDPOINT_TYPES = frozenset({"image-generation"})
VIDEO_ENDPOINT_TYPES = frozenset({"openai-video"})
RERANK_ENDPOINT_TYPES = frozenset({"jina-rerank"})

CAPABILITIES = ("chat", "chat-image", "embedding", "image", "video", "rerank", "all")
_QUERY_CAPABILITY = {"chat": "chat", "chat-image": "chat", "embedding": "embedding", "image": "image"}
_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]*(/[A-Za-z0-9][A-Za-z0-9._:-]*)+$")

_AUTH_HINT = (
    "OrcaRouter rejected the API key (HTTP 401). Create or rotate a key at "
    f"{ORCAROUTER_KEY_URL} and set llm.api_key or {ORCAROUTER_KEY_ENV}."
)
_PERMISSION_HINT = (
    "OrcaRouter denied this request (HTTP 403). The key may not be enabled for the requested "
    f"model; security-tier access is described at {ORCAROUTER_CYBER_MODELS_URL}."
)
_RATE_LIMIT_HINT = "OrcaRouter rate-limited this request (HTTP 429). Retry later; the key was not changed."


class OrcaRouterError(RuntimeError):
    """Base class for OrcaRouter errors safe to show to the user."""


class OrcaRouterAuthError(OrcaRouterError):
    """Terminal credential failure; the stored key needs to be replaced."""


class OrcaRouterRateLimitError(OrcaRouterError):
    """The gateway asked the client to slow down."""


class OrcaRouterCatalogError(OrcaRouterError):
    """The model catalog could not be fetched or parsed."""


@dataclass(frozen=True)
class OrcaModel:
    """A single model record from the OrcaRouter catalog.

    Model identifiers keep their ``vendor/model`` namespace verbatim. Metadata
    that the catalog does not declare stays empty so capability filters can
    fail closed instead of guessing from the model name.
    """

    id: str
    name: str
    context_length: int | None
    input_modalities: tuple[str, ...]
    output_modalities: tuple[str, ...]
    supported_endpoint_types: tuple[str, ...]
    reasoning_efforts: tuple[str, ...]
    source: str

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation for CLI output."""
        return {
            "id": self.id,
            "name": self.name,
            "context_length": self.context_length,
            "input_modalities": list(self.input_modalities),
            "output_modalities": list(self.output_modalities),
            "supported_endpoint_types": list(self.supported_endpoint_types),
            "reasoning_efforts": list(self.reasoning_efforts),
            "source": self.source,
        }


@dataclass(frozen=True)
class ModelCatalog:
    """Result of a catalog lookup, including its provenance."""

    models: tuple[OrcaModel, ...]
    source: str
    degraded: bool
    error: str | None

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serialisable representation for CLI output."""
        return {
            "provider": ORCAROUTER_PROVIDER,
            "display_name": ORCAROUTER_DISPLAY_NAME,
            "source": self.source,
            "degraded": self.degraded,
            "error": self.error,
            "models": [model.to_dict() for model in self.models],
        }


def _nonempty(value: str | None) -> str | None:
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _validate_base_url(value: str, *, name: str) -> str:
    """Validate an OrcaRouter origin and return it without a trailing slash."""
    candidate = value.strip()
    parsed = urllib.parse.urlsplit(candidate)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError(f"{name} must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise ValueError(f"{name} must not include userinfo")
    if parsed.query or parsed.fragment:
        raise ValueError(f"{name} must not include a query or fragment")
    if parsed.scheme == "http" and parsed.hostname.lower() not in _LOOPBACK_HOSTS:
        raise ValueError(f"{name} must use HTTPS unless it points at loopback")
    return candidate.rstrip("/")


def resolve_auth_base(config_base: str | None = None) -> str:
    """Resolve the account/authorization origin.

    Priority: explicit config, ``ORCA_AUTH_BASE_URL``, shared
    ``ORCA_BASE_URL``, then the public default. Never derived from the
    inference origin.
    """
    explicit = _nonempty(config_base) or _nonempty(os.environ.get(ORCA_AUTH_BASE_ENV))
    if explicit:
        return _validate_base_url(explicit, name="OrcaRouter auth base URL")
    shared = _nonempty(os.environ.get(ORCA_BASE_ENV))
    if shared:
        return _validate_base_url(shared, name="ORCA_BASE_URL")
    return ORCAROUTER_DEFAULT_AUTH_BASE


def resolve_api_base(config_base: str | None = None) -> str:
    """Resolve the inference/model-discovery origin.

    Priority: explicit config or ``ORCA_API_BASE_URL``, shared
    ``ORCA_BASE_URL`` (with ``/v1`` added when it has no path), then the public
    default. Authentication is never derived from this value.
    """
    explicit = _nonempty(config_base) or _nonempty(os.environ.get(ORCA_API_BASE_ENV))
    if explicit:
        return _validate_base_url(explicit, name="OrcaRouter API base URL")
    shared = _nonempty(os.environ.get(ORCA_BASE_ENV))
    if shared:
        base = _validate_base_url(shared, name="ORCA_BASE_URL")
        if urllib.parse.urlsplit(base).path in {"", "/"}:
            base = base + "/v1"
        return base
    return ORCAROUTER_DEFAULT_API_BASE


def resolve_api_key(config_key: str | None = None) -> str:
    """Return the configured OrcaRouter key without ever echoing it.

    Uses the project's existing ``llm.api_key`` slot first and the named
    ``ORCAROUTER_API_KEY`` environment variable as a fallback. Unrelated
    provider variables are deliberately ignored.
    """
    key = _nonempty(config_key) or _nonempty(os.environ.get(ORCAROUTER_KEY_ENV))
    if key is None:
        raise OrcaRouterAuthError(
            "No OrcaRouter API key configured. Set llm.api_key or "
            f"{ORCAROUTER_KEY_ENV}; create a key at {ORCAROUTER_KEY_URL}."
        )
    return key


def _string_tuple(value: Any) -> tuple[str, ...]:
    if not isinstance(value, list):
        return ()
    return tuple(str(item).strip().lower() for item in value if isinstance(item, str) and item.strip())


def _reasoning_efforts(item: dict[str, Any]) -> tuple[str, ...]:
    for key in ("reasoning_efforts", "supported_efforts"):
        values = _string_tuple(item.get(key))
        if values:
            return values
    reasoning = item.get("reasoning")
    if isinstance(reasoning, dict):
        for key in ("efforts", "supported_efforts", "levels"):
            values = _string_tuple(reasoning.get(key))
            if values:
                return values
    return ()


def _parse_model(item: Any, *, source: str) -> OrcaModel | None:
    if not isinstance(item, dict):
        return None
    model_id = item.get("id")
    if not isinstance(model_id, str) or not _MODEL_ID_RE.match(model_id):
        return None
    architecture = item.get("architecture")
    input_modalities: tuple[str, ...] = ()
    output_modalities: tuple[str, ...] = ()
    if isinstance(architecture, dict):
        input_modalities = _string_tuple(architecture.get("input_modalities"))
        output_modalities = _string_tuple(architecture.get("output_modalities"))
    context_length = item.get("context_length")
    if not isinstance(context_length, int) or isinstance(context_length, bool) or context_length <= 0:
        context_length = None
    name = item.get("name")
    return OrcaModel(
        id=model_id,
        name=name if isinstance(name, str) and name.strip() else model_id,
        context_length=context_length,
        input_modalities=input_modalities,
        output_modalities=output_modalities,
        supported_endpoint_types=_string_tuple(item.get("supported_endpoint_types")),
        reasoning_efforts=_reasoning_efforts(item),
        source=source,
    )


def parse_catalog(payload: Any, *, source: str = "live") -> tuple[OrcaModel, ...]:
    """Parse an OpenAI-style model list, bounded and fail-closed."""
    if not isinstance(payload, dict):
        raise OrcaRouterCatalogError("OrcaRouter model catalog returned an unexpected payload.")
    items = payload.get("data")
    if not isinstance(items, list):
        raise OrcaRouterCatalogError("OrcaRouter model catalog did not include a model list.")
    models: list[OrcaModel] = []
    for item in items[:CATALOG_MAX_MODELS]:
        model = _parse_model(item, source=source)
        if model is not None:
            models.append(model)
    return tuple(models)


def _supports_chat(model: OrcaModel) -> bool:
    endpoint_types = set(model.supported_endpoint_types)
    return bool(endpoint_types & CHAT_ENDPOINT_TYPES)


def _supports_chat_image(model: OrcaModel) -> bool:
    return _supports_chat(model) and "image" in model.input_modalities


_CAPABILITY_FILTERS: dict[str, Callable[[OrcaModel], bool]] = {
    "chat": _supports_chat,
    "chat-image": _supports_chat_image,
    "embedding": lambda model: bool(set(model.supported_endpoint_types) & EMBEDDING_ENDPOINT_TYPES),
    "image": lambda model: bool(set(model.supported_endpoint_types) & IMAGE_ENDPOINT_TYPES),
    "video": lambda model: bool(set(model.supported_endpoint_types) & VIDEO_ENDPOINT_TYPES),
    "rerank": lambda model: bool(set(model.supported_endpoint_types) & RERANK_ENDPOINT_TYPES),
}


def filter_models(models: Iterable[OrcaModel], capability: str) -> tuple[OrcaModel, ...]:
    """Filter models for one entry point using declared catalog metadata only."""
    if capability == "all":
        return tuple(models)
    matcher = _CAPABILITY_FILTERS.get(capability)
    if matcher is None:
        raise ValueError(f"Unknown OrcaRouter capability: {capability!r}")
    return tuple(model for model in models if matcher(model))


def _verified_seed() -> tuple[OrcaModel, ...]:
    """Small, verified outage fallback. Live discovery always wins."""
    return (
        OrcaModel(
            id="openai/gpt-5.5",
            name="GPT-5.5",
            context_length=400_000,
            input_modalities=("text", "image"),
            output_modalities=("text",),
            supported_endpoint_types=("openai", "openai-response", "anthropic"),
            reasoning_efforts=("low", "medium", "high", "xhigh"),
            source="seed",
        ),
        OrcaModel(
            id="anthropic/claude-opus-4.8",
            name="Claude Opus 4.8",
            context_length=200_000,
            input_modalities=("text", "image"),
            output_modalities=("text",),
            supported_endpoint_types=("openai", "openai-response", "anthropic"),
            reasoning_efforts=(),
            source="seed",
        ),
        OrcaModel(
            id="google/gemini-3.5-flash",
            name="Gemini 3.5 Flash",
            context_length=1_000_000,
            input_modalities=("text", "image", "audio", "video"),
            output_modalities=("text",),
            supported_endpoint_types=("openai", "openai-response", "gemini"),
            reasoning_efforts=(),
            source="seed",
        ),
        OrcaModel(
            id="deepseek/deepseek-v4-pro",
            name="DeepSeek V4 Pro",
            context_length=1_048_576,
            input_modalities=("text",),
            output_modalities=("text",),
            supported_endpoint_types=("openai", "openai-response"),
            reasoning_efforts=(),
            source="seed",
        ),
        OrcaModel(
            id=ORCAROUTER_DEFAULT_MODEL,
            name="OrcaRouter Auto",
            context_length=None,
            input_modalities=("text",),
            output_modalities=("text",),
            supported_endpoint_types=("openai", "openai-response", "anthropic", "gemini"),
            reasoning_efforts=(),
            source="seed",
        ),
        OrcaModel(
            id=ORCAROUTER_FALLBACK_MODEL,
            name="OrcaRouter Fusion",
            context_length=1_000_000,
            input_modalities=("text",),
            output_modalities=("text",),
            supported_endpoint_types=("openai", "openai-response", "anthropic", "gemini"),
            reasoning_efforts=(),
            source="seed",
        ),
        OrcaModel(
            id="orca/orcacyber-ultra-1.0",
            name="OrcaCyber Ultra",
            context_length=1_048_576,
            input_modalities=("text",),
            output_modalities=("text",),
            supported_endpoint_types=("openai",),
            reasoning_efforts=(),
            source="seed",
        ),
        OrcaModel(
            id="orca/orcacyber-zero-1.5",
            name="OrcaCyber Zero",
            context_length=1_000_000,
            input_modalities=("text",),
            output_modalities=("text",),
            supported_endpoint_types=("openai",),
            reasoning_efforts=(),
            source="seed",
        ),
    )


VERIFIED_SEED: tuple[OrcaModel, ...] = _verified_seed()


def _catalog_url(base_url: str, capability: str) -> str:
    url = base_url + "/models"
    query_capability = _QUERY_CAPABILITY.get(capability)
    if query_capability is None:
        return url
    return url + "?" + urllib.parse.urlencode({"capability": query_capability})


def _fetch_catalog(url: str, api_key: str, timeout_s: float) -> tuple[OrcaModel, ...]:
    """Fetch and parse the live catalog with bounded time, size, and count."""
    request = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {api_key}", "Accept": "application/json"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            raw = response.read(CATALOG_MAX_BYTES + 1)
    except urllib.error.HTTPError as exc:
        if exc.code in {401, 403}:
            raise OrcaRouterAuthError(_AUTH_HINT) from None
        if exc.code == 429:
            raise OrcaRouterRateLimitError(_RATE_LIMIT_HINT) from None
        raise OrcaRouterCatalogError(f"OrcaRouter model catalog request failed (HTTP {exc.code}).") from None
    except (urllib.error.URLError, TimeoutError, OSError):
        raise OrcaRouterCatalogError("Could not reach the OrcaRouter model catalog (network error).") from None
    if len(raw) > CATALOG_MAX_BYTES:
        raise OrcaRouterCatalogError("OrcaRouter model catalog response exceeded the size limit.")
    try:
        payload = json.loads(raw)
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise OrcaRouterCatalogError("OrcaRouter model catalog returned invalid JSON.") from None
    return parse_catalog(payload)


def discover_models(
    api_key: str | None = None,
    base_url: str | None = None,
    capability: str = "chat",
    timeout_s: float = CATALOG_TIMEOUT_S,
    fetcher: Callable[[str, str, float], tuple[OrcaModel, ...]] | None = None,
) -> ModelCatalog:
    """Discover models for one capability.

    A successful live response is authoritative and is never mixed with the
    seed. Credential failures are terminal and propagate. Transport, HTTP, and
    parsing failures fall back to the small verified seed and are marked
    degraded so callers can show a refresh hint.
    """
    if capability not in CAPABILITIES:
        raise ValueError(f"Unknown OrcaRouter capability: {capability!r}")
    key = resolve_api_key(api_key)
    base = resolve_api_base(base_url)
    bounded_timeout = min(max(float(timeout_s), 1.0), 30.0)
    fetch = fetcher or _fetch_catalog
    try:
        models = fetch(_catalog_url(base, capability), key, bounded_timeout)
    except (OrcaRouterAuthError, OrcaRouterRateLimitError):
        raise
    except OrcaRouterCatalogError as exc:
        return ModelCatalog(
            models=filter_models(VERIFIED_SEED, capability),
            source="seed",
            degraded=True,
            error=str(exc),
        )
    return ModelCatalog(models=filter_models(models, capability), source="live", degraded=False, error=None)


class OrcaRouterProvider(OpenAIProvider):
    """OrcaRouter Cyber Inference provider.

    Uses the OpenAI-compatible chat completions wire format at
    ``https://api.orcarouter.ai/v1``. Credentials are the user's own
    ``sk-orca-…`` key supplied through the project's existing config/env
    mechanism; there is no refresh grant, so a rejected key is terminal and
    must be replaced.
    """

    def __init__(
        self,
        api_key: str | None = None,
        model: str = ORCAROUTER_DEFAULT_MODEL,
        max_tokens: int = 4096,
        temperature: float = 0.0,
        timeout_s: int = 1800,
        base_url: str | None = None,
    ) -> None:
        resolved_key = resolve_api_key(api_key)
        resolved_base = resolve_api_base(base_url)
        super().__init__(
            api_key=resolved_key,
            model=model or ORCAROUTER_DEFAULT_MODEL,
            max_tokens=max_tokens,
            temperature=temperature,
            timeout_s=timeout_s,
            base_url=resolved_base,
        )
        self.base_url = resolved_base
        self.provider_name = ORCAROUTER_DISPLAY_NAME

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        """Send a chat request and translate gateway failures into safe errors."""
        try:
            text = super().send(messages, **kwargs)
        except openai.AuthenticationError:
            raise OrcaRouterAuthError(_AUTH_HINT) from None
        except openai.PermissionDeniedError:
            raise OrcaRouterAuthError(_PERMISSION_HINT) from None
        except openai.RateLimitError:
            raise OrcaRouterRateLimitError(_RATE_LIMIT_HINT) from None
        self.last_metadata = {
            **self.last_metadata,
            "provider": ORCAROUTER_PROVIDER,
            "provider_name": ORCAROUTER_DISPLAY_NAME,
            "base_url": self.base_url,
        }
        return text
