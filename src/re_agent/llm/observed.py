"""Shared call budgets and complete per-call audit trails."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, is_dataclass
from pathlib import Path
from typing import Any

from re_agent.llm.protocol import LLMProvider, Message


@dataclass
class CallBudget:
    limit: int
    used: int = 0

    def consume(self) -> int:
        if self.used >= self.limit:
            raise RuntimeError(f"LLM call budget exhausted ({self.used}/{self.limit})")
        self.used += 1
        return self.used


class ObservedProvider:
    def __init__(self, provider: LLMProvider, budget: CallBudget, role: str, log_dir: Path | None) -> None:
        self.provider = provider
        self.budget = budget
        self.role = role
        self.log_dir = log_dir

    @property
    def last_metadata(self) -> object:
        return getattr(self.provider, "last_metadata", {})

    @property
    def supports_conversations(self) -> bool:
        return self.provider.supports_conversations

    def new_conversation(self, system: str) -> str:
        return self.provider.new_conversation(system)

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        return self._call(messages=messages, kwargs=kwargs)

    def resume(self, conversation_id: str, message: str) -> str:
        return self._call(conversation_id=conversation_id, message=message)

    def _call(self, **request: Any) -> str:
        from re_agent.orchestrator.execution import current, progress

        progress(self.role)
        context = current()
        number = self.budget.consume()
        if context:
            context.emit("call", self.role)
        start = time.monotonic()
        event: dict[str, Any] = {"role": self.role, "call": number, "request": request}
        try:
            if "messages" in request:
                response = self.provider.send(request["messages"], **request["kwargs"])
            else:
                response = self.provider.resume(request["conversation_id"], request["message"])
            event["response"] = response
            if context:
                context.check()
            return response
        except Exception as exc:
            event["error"] = str(exc)
            raise
        finally:
            event["duration_s"] = time.monotonic() - start
            event["metadata"] = self.last_metadata
            if self.log_dir:

                def encode(value: Any) -> Any:
                    if is_dataclass(value) and not isinstance(value, type):
                        return asdict(value)
                    return str(value)

                (self.log_dir / f"call-{number:04d}-{self.role}.json").write_text(
                    json.dumps(event, default=encode, indent=2), encoding="utf-8"
                )
