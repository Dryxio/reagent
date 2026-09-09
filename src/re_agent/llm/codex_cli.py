"""Codex CLI-backed LLM provider using ChatGPT login credentials."""
from __future__ import annotations

import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from re_agent.llm.protocol import Message


class CodexCLIProvider:
    """LLM provider backed by the local ``codex exec`` CLI."""

    def __init__(
        self,
        model: str = "gpt-5.4",
        timeout_s: int = 1800,
        codex_bin: str = "codex",
    ) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._codex_bin = codex_bin
        self._conversations: dict[str, list[Message]] = {}

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        prompt = self._render_messages(messages)
        model = kwargs.get("model", self._model)
        with tempfile.NamedTemporaryFile("r+", encoding="utf-8", delete=False) as tmp:
            out_path = Path(tmp.name)

        try:
            proc = subprocess.run(
                [
                    self._codex_bin,
                    "exec",
                    "-s",
                    "read-only",
                    "--color",
                    "never",
                    "--skip-git-repo-check",
                    "--output-last-message",
                    str(out_path),
                    "-m",
                    str(model),
                    "-",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                input=prompt,
                text=True,
                encoding="utf-8",
                timeout=self._timeout_s,
                check=False,
            )
            if proc.returncode != 0:
                raise RuntimeError(
                    f"codex exec failed with exit code {proc.returncode}\n{proc.stdout}"
                )
            return out_path.read_text(encoding="utf-8")
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"codex exec timed out after {self._timeout_s}s") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"codex CLI not found: {self._codex_bin}") from exc
        finally:
            out_path.unlink(missing_ok=True)

    @property
    def supports_conversations(self) -> bool:
        return True

    def new_conversation(self, system: str) -> str:
        cid = uuid.uuid4().hex
        self._conversations[cid] = [Message(role="system", content=system)]
        return cid

    def resume(self, conversation_id: str, message: str) -> str:
        history = self._conversations.get(conversation_id)
        if history is None:
            raise KeyError(f"Unknown conversation ID: {conversation_id}")

        user_message = Message(role="user", content=message)
        response_text = self.send([*history, user_message])
        history.extend([user_message, Message(role="assistant", content=response_text)])
        return response_text

    @staticmethod
    def _render_messages(messages: list[Message]) -> str:
        parts: list[str] = []
        for msg in messages:
            role = msg.role.upper()
            parts.append(f"[{role}]\n{msg.content.strip()}")
        return "\n\n".join(parts).strip()
