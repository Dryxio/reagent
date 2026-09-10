"""Shared call budgets and complete per-call audit trails."""

from __future__ import annotations

import json
import time
from contextlib import nullcontext, suppress
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

        context = current()
        retries = context.request_retries if context else 0
        for attempt in range(retries + 1):
            progress("waiting-for-model")
            admission = context.requests.acquire(context) if context and context.requests else nullcontext(0.0)
            try:
                with admission as waited:
                    return self._call_once(waited, **request)
            except Exception as exc:
                # Only explicit rate limits are safe to retry automatically.
                if getattr(exc, "status_code", None) != 429:
                    raise
                delay = min(60.0, 2.0 ** attempt)
                with suppress(AttributeError, TypeError, ValueError):
                    headers = getattr(getattr(exc, "response", None), "headers", {})
                    delay = min(60.0, max(delay, float(headers.get("retry-after", delay))))
                if context and context.requests:
                    context.requests.defer(delay)
                if attempt == retries:
                    raise
                progress("rate-limit-backoff")
                if context and context.cancel.wait(delay):
                    context.check()
        raise AssertionError("unreachable")

    def _call_once(self, waited: float, **request: Any) -> str:
        from re_agent.orchestrator.execution import current, progress

        progress(self.role)
        context = current()
        number = self.budget.consume()
        if context:
            context.emit("call", self.role)
        start = time.monotonic()
        event: dict[str, Any] = {"role": self.role, "call": number, "request": request, "queue_wait_s": waited}
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
            status = getattr(exc, "status_code", None)
            if context and status in (401, 403):
                context.fail("authentication", str(exc))
            event["error"] = str(exc)
            raise
        finally:
            event["duration_s"] = time.monotonic() - start
            event["metadata"] = self.last_metadata
            if context:
                context.emit("request_timing", {"queue_wait_s": waited, "request_s": event["duration_s"]})
            if self.log_dir:

                def encode(value: Any) -> Any:
                    if is_dataclass(value) and not isinstance(value, type):
                        return asdict(value)
                    return str(value)

                (self.log_dir / f"call-{number:04d}-{self.role}.json").write_text(
                    json.dumps(event, default=encode, indent=2), encoding="utf-8"
                )
