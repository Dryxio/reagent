"""Claude Code CLI provider using an existing Claude subscription login."""
from __future__ import annotations

import json
import subprocess
import uuid
from dataclasses import dataclass, field
from typing import Any

from re_agent.llm.protocol import Message
from re_agent.utils.process import run_process


@dataclass
class _Conversation:
    system: str
    started: bool = False


@dataclass
class ClaudeCLIMetadata:
    """Usage metadata returned by Claude Code print mode."""

    session_id: str | None = None
    cost_usd: float | None = None
    duration_ms: int | None = None
    usage: dict[str, Any] = field(default_factory=dict)


class ClaudeCLIProvider:
    """Run Claude Code in non-interactive, tool-free print mode."""

    def __init__(
        self,
        model: str = "sonnet",
        timeout_s: int = 1800,
        claude_bin: str = "claude",
        max_budget_usd: float | None = None,
        effort: str | None = None,
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._claude_bin = claude_bin
        self._max_budget_usd = max_budget_usd
        self._effort = effort
        self._conversations: dict[str, _Conversation] = {}
        self.last_metadata = ClaudeCLIMetadata()

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        prompt = self._render_messages([m for m in messages if m.role != "system"])
        return self._run(prompt, system=system or None, model=kwargs.get("model"))

    @property
    def supports_conversations(self) -> bool:
        return True

    def new_conversation(self, system: str) -> str:
        conversation_id = str(uuid.uuid4())
        self._conversations[conversation_id] = _Conversation(system=system)
        return conversation_id

    def resume(self, conversation_id: str, message: str) -> str:
        conversation = self._conversations.get(conversation_id)
        if conversation is None:
            raise KeyError(f"Unknown conversation ID: {conversation_id}")

        if conversation.started:
            result = self._run(message, resume=conversation_id)
        else:
            result = self._run(
                message,
                system=conversation.system,
                session_id=conversation_id,
            )
            conversation.started = True
        return result

    def _run(
        self,
        prompt: str,
        *,
        system: str | None = None,
        session_id: str | None = None,
        resume: str | None = None,
        model: str | None = None,
    ) -> str:
        cmd = [
            self._claude_bin,
            "-p",
            "--tools",
            "",
            "--disallowedTools",
            "mcp__*",
            "--output-format",
            "json",
            "--model",
            str(model or self._model),
        ]
        if system is not None:
            cmd.extend(["--system-prompt", system])
        if session_id is not None:
            cmd.extend(["--session-id", session_id])
        if resume is not None:
            cmd.extend(["--resume", resume])
        if self._max_budget_usd is not None:
            cmd.extend(["--max-budget-usd", str(self._max_budget_usd)])
        if self._effort is not None:
            cmd.extend(["--effort", self._effort])
        cmd.append(prompt)

        try:
            proc = run_process(
                cmd,
                timeout_s=self._timeout_s,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"claude CLI timed out after {self._timeout_s}s") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"Claude CLI not found: {self._claude_bin}") from exc

        try:
            payload = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            if proc.returncode != 0:
                detail = proc.stderr.strip() or proc.stdout.strip()
                raise RuntimeError(
                    f"claude CLI failed with exit code {proc.returncode}\n{detail}"
                ) from exc
            raise RuntimeError("claude CLI returned invalid JSON") from exc
        if not isinstance(payload, dict):
            raise RuntimeError("claude CLI returned an unexpected JSON payload")

        self.last_metadata = ClaudeCLIMetadata(
            session_id=_string_or_none(payload.get("session_id")),
            cost_usd=_float_or_none(payload.get("total_cost_usd")),
            duration_ms=_int_or_none(payload.get("duration_ms")),
            usage=payload.get("usage", {}) if isinstance(payload.get("usage"), dict) else {},
        )
        result = payload.get("result")
        if proc.returncode != 0 or payload.get("is_error") is True:
            detail = result if isinstance(result, str) else (proc.stderr.strip() or "unknown error")
            raise RuntimeError(f"claude CLI request failed: {detail}")
        if not isinstance(result, str):
            raise RuntimeError("claude CLI JSON payload has no text result")
        return result

    @staticmethod
    def _render_messages(messages: list[Message]) -> str:
        return "\n\n".join(f"[{m.role.upper()}]\n{m.content.strip()}" for m in messages).strip()


def _string_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _float_or_none(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _int_or_none(value: object) -> int | None:
    return int(value) if isinstance(value, (int, float)) else None
