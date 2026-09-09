"""Regression tests for Codex conversation recovery."""
from __future__ import annotations

import pytest

from re_agent.llm.codex_cli import CodexCLIProvider


def test_failed_turn_does_not_duplicate_prompt_on_retry(monkeypatch):
    provider = CodexCLIProvider()
    conversation = provider.new_conversation("system")
    requests = []

    def send(messages, **kwargs):
        requests.append(messages)
        if len(requests) == 1:
            raise RuntimeError("CLI unavailable")
        return "answer"

    monkeypatch.setattr(provider, "send", send)
    with pytest.raises(RuntimeError, match="CLI unavailable"):
        provider.resume(conversation, "request")
    assert provider.resume(conversation, "request") == "answer"
    assert [message.content for message in requests[1]] == ["system", "request"]
    provider.resume(conversation, "next")
    assert [message.content for message in requests[2]] == ["system", "request", "answer", "next"]
