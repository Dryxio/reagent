"""Grok Build headless provider using the local CLI login."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
import uuid
from pathlib import Path
from typing import Any

from re_agent.llm.protocol import Message
from re_agent.utils.process import run_process


class GrokCLIProvider:
    """Run tool-free Grok requests in an isolated workspace, retaining sessions."""

    def __init__(self, model: str = "", timeout_s: int = 1800,
                 grok_bin: str = "grok", effort: str | None = None) -> None:
        self._model = model
        self._timeout_s = timeout_s
        self._grok_bin = grok_bin
        self._effort = effort
        self._workspace = tempfile.TemporaryDirectory(prefix="re-agent-grok-")
        self._env = self._isolated_environment(Path(self._workspace.name))
        self._conversations: dict[str, tuple[str, bool]] = {}
        self.last_metadata: dict[str, Any] = {}

    @staticmethod
    def _isolated_environment(workspace: Path) -> dict[str, str]:
        """Use Grok's supported home/auth overrides without copying credentials."""
        original = Path(os.environ.get("GROK_HOME", str(Path.home() / ".grok"))).resolve()
        home = workspace / "grok-home"
        home.mkdir(mode=0o700)
        # Keep local managed requirements as well as system-level policies.
        for name in ("requirements.toml", "managed_config.toml"):
            source = original / name
            if source.is_file():
                shutil.copy2(source, home / name)
        settings = "[cli]\nauto_update = false\nuse_leader = false\n"
        settings += "[models]\nmax_retries = 0\n"
        settings += "[managed_mcps]\nenabled = false\ngateway_tools_enabled = false\n"
        for vendor in ("claude", "cursor"):
            settings += f"[compat.{vendor}]\n"
            settings += "".join(f"{surface} = false\n" for surface in
                                ("mcps", "skills", "rules", "agents", "hooks", "sessions"))
        (home / "config.toml").write_text(settings, encoding="utf-8")
        return {**os.environ, "GROK_HOME": str(home),
                "GROK_AUTH_PATH": os.environ.get("GROK_AUTH_PATH", str(original / "auth.json"))}

    @property
    def supports_conversations(self) -> bool:
        return True

    def new_conversation(self, system: str) -> str:
        cid = str(uuid.uuid4())
        self._conversations[cid] = (system, False)
        return cid

    def resume(self, conversation_id: str, message: str) -> str:
        if conversation_id not in self._conversations:
            raise KeyError(f"Unknown conversation ID: {conversation_id}")
        system, started = self._conversations[conversation_id]
        text = self._run(message, system=system if not started else None,
                         session_id=conversation_id if not started else None,
                         resume=conversation_id if started else None)
        self._conversations[conversation_id] = (system, True)
        return text

    def close(self) -> None:
        self._workspace.cleanup()

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        system = "\n\n".join(m.content for m in messages if m.role == "system")
        prompt = "\n\n".join(f"[{m.role.upper()}]\n{m.content}" for m in messages if m.role != "system")
        return self._run(prompt, system=system or None, model=kwargs.get("model"))

    def _run(self, prompt: str, *, system: str | None = None, session_id: str | None = None,
             resume: str | None = None, model: str | None = None) -> str:
        self.last_metadata = {}
        path = Path(self._workspace.name) / f"prompt-{uuid.uuid4().hex}.txt"
        path.write_text(prompt, encoding="utf-8")
        command = [self._grok_bin, "--prompt-file", str(path), "--output-format", "json",
                   # Empty --tools is not reliably tool-free in Grok Build. Select a
                   # real tool, remove it, and remove always-on MCP meta-tools.
                   "--tools", "read_file", "--disallowed-tools", "read_file,search_tool,use_tool",
                   "--deny", "*", "--no-subagents", "--disable-web-search",
                   "--max-turns", "1"]
        for flag, value in [("--model", model if model is not None else self._model),
                            ("--system-prompt-override", system), ("--session-id", session_id),
                            ("--resume", resume), ("--effort", self._effort)]:
            if value:
                command.extend([flag, value])
        try:
            proc = run_process(command, cwd=self._workspace.name, env=self._env, timeout_s=self._timeout_s)
            try:
                payload = json.loads(proc.stdout)
            except json.JSONDecodeError as exc:
                if proc.returncode:
                    raise RuntimeError(
                        f"Grok CLI failed with exit code {proc.returncode}: {proc.stderr[-2000:]}"
                    ) from exc
                raise RuntimeError("Grok CLI returned invalid JSON") from exc
            if not isinstance(payload, dict):
                raise RuntimeError("Grok CLI returned an unexpected JSON payload")
            self.last_metadata = {key: payload[key] for key in
                                  ("sessionId", "requestId", "usage", "modelUsage", "total_cost_usd",
                                   "stopReason", "num_turns", "type", "message")
                                  if key in payload}
            if proc.returncode:
                detail = payload.get("message") or proc.stderr.strip() or "request failed"
                raise RuntimeError(f"Grok CLI failed with exit code {proc.returncode}: {str(detail)[-2000:]}")
            if (payload.get("type") == "error" or payload.get("error") or payload.get("is_error")
                    or payload.get("stopReason") != "end_turn"):
                raise RuntimeError(f"Grok CLI did not complete a text response: {payload.get('stopReason', 'error')}")
            text = payload.get("text")
            if not isinstance(text, str) or not text.strip():
                raise RuntimeError("Grok CLI JSON payload has no text response")
            expected = session_id or resume
            if expected and payload.get("sessionId") != expected:
                raise RuntimeError("Grok CLI returned a different session ID")
            return text
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(f"Grok CLI timed out after {self._timeout_s}s") from exc
        except FileNotFoundError as exc:
            raise RuntimeError(f"Grok CLI not found: {self._grok_bin}") from exc
        finally:
            path.unlink(missing_ok=True)
