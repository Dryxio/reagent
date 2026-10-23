"""Live OrcaRouter Cyber Inference checks; require ORCAROUTER_API_KEY."""

from __future__ import annotations

import os
import urllib.parse

import pytest

from re_agent.llm.orcarouter import (
    ORCAROUTER_KEY_ENV,
    OrcaRouterAuthError,
    OrcaRouterProvider,
    discover_models,
)
from re_agent.llm.protocol import Message

pytestmark = pytest.mark.llm

CYBER_MODEL = "orca/orcacyber-ultra-1.0"
VERIFICATION_MODEL = "deepseek/deepseek-v4.1-flash"


def _api_key() -> str:
    key = os.environ.get(ORCAROUTER_KEY_ENV, "").strip()
    if not key:
        pytest.skip(f"{ORCAROUTER_KEY_ENV} is not configured")
    return key


@pytest.fixture(autouse=True)
def _drop_socks_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """Keep direct HTTPS for the API host when only a SOCKS proxy is exported.

    ``api.orcarouter.ai`` is in the runner's NO_PROXY list; the OpenAI client
    still tries to build a SOCKS transport from the environment and requires an
    optional dependency for it. Other proxy schemes are left untouched.
    """
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "http_proxy", "https_proxy", "all_proxy"):
        value = os.environ.get(name, "")
        if value and urllib.parse.urlsplit(value).scheme.startswith("socks"):
            monkeypatch.delenv(name, raising=False)


def test_live_catalog_and_chat_through_provider() -> None:
    key = _api_key()
    catalog = discover_models(api_key=key, capability="chat", timeout_s=20)
    assert catalog.source == "live", f"catalog fell back to {catalog.source}: {catalog.error}"
    assert catalog.models, "live catalog returned no chat models"
    assert all("/" in model.id for model in catalog.models)
    model_ids = {model.id for model in catalog.models}
    assert "orcarouter/auto" in model_ids

    model = CYBER_MODEL if CYBER_MODEL in model_ids else VERIFICATION_MODEL
    provider = OrcaRouterProvider(api_key=key, model=model, max_tokens=32, timeout_s=90)
    try:
        text = provider.send([Message(role="user", content="Reply with the single word: ready")])
    except OrcaRouterAuthError:
        if model == VERIFICATION_MODEL:
            raise
        provider = OrcaRouterProvider(api_key=key, model=VERIFICATION_MODEL, max_tokens=32, timeout_s=90)
        text = provider.send([Message(role="user", content="Reply with the single word: ready")])

    assert text.strip()
    assert provider.last_metadata["provider"] == "orcarouter"
    assert "api.orcarouter.ai" in provider.base_url
    assert key not in text
