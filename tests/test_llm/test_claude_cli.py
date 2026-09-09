"""Tests for the subscription-backed Claude Code CLI provider."""
from __future__ import annotations

import json
from subprocess import CompletedProcess
from unittest.mock import patch

import pytest

from re_agent.llm.claude_cli import ClaudeCLIProvider
from re_agent.llm.protocol import Message


def _completed(session_id: str = "session-1") -> CompletedProcess[str]:
    payload = {
        "result": "generated code",
        "session_id": session_id,
        "total_cost_usd": 0.12,
        "duration_ms": 42,
        "usage": {"input_tokens": 10, "output_tokens": 4},
    }
    return CompletedProcess(["claude"], 0, stdout=json.dumps(payload), stderr="")


def test_claude_cli_send_parses_result_and_usage() -> None:
    provider = ClaudeCLIProvider(model="sonnet", max_budget_usd=1.5, effort="high")
    with patch("re_agent.llm.claude_cli.run_process", return_value=_completed()) as run:
        result = provider.send([
            Message(role="system", content="system"),
            Message(role="user", content="reverse this"),
        ])

    assert result == "generated code"
    assert provider.last_metadata.cost_usd == 0.12
    command = run.call_args.args[0]
    assert "--tools" in command
    assert "--bare" not in command
    assert "--max-budget-usd" in command
    assert "--system-prompt" in command


def test_claude_cli_uses_real_session_resume() -> None:
    provider = ClaudeCLIProvider()
    conversation_id = provider.new_conversation("system")
    with patch("re_agent.llm.claude_cli.run_process", return_value=_completed()) as run:
        provider.resume(conversation_id, "first")
        first = run.call_args.args[0]
        provider.resume(conversation_id, "second")
        second = run.call_args.args[0]

    assert "--session-id" in first
    assert "--resume" in second


def test_claude_cli_surfaces_structured_error() -> None:
    payload = json.dumps({"is_error": True, "result": "Not logged in", "session_id": "s"})
    completed = CompletedProcess(["claude"], 1, stdout=payload, stderr="")
    provider = ClaudeCLIProvider()
    with (
        patch("re_agent.llm.claude_cli.run_process", return_value=completed),
        pytest.raises(RuntimeError, match="Not logged in"),
    ):
        provider.send([Message(role="user", content="hello")])
