"""Manifest selection over the existing cumulative class runner."""
from __future__ import annotations

from typing import Any, cast

from re_agent.backend.protocol import REBackend
from re_agent.config.schema import ReAgentConfig
from re_agent.core.models import FunctionEntry, ReversalResult
from re_agent.core.session import Session
from re_agent.core.target_plan import TargetPlan
from re_agent.llm.protocol import LLMProvider
from re_agent.orchestrator.class_runner import reverse_class


class _ManifestBackend:
    """Restrict enumeration to the manifest; delegate analysis to the real backend."""

    def __init__(self, backend: REBackend, plan: TargetPlan) -> None:
        self.backend = backend
        self.plan = plan

    def __getattr__(self, name: str) -> Any:
        return getattr(self.backend, name)

    def remaining(self, class_name: str | None = None) -> list[FunctionEntry]:
        return [FunctionEntry(f.address, f.function_name, f.class_name, f.caller_count) for f in self.plan.functions]

    def unimplemented(self, filter_pattern: str | None = None) -> list[FunctionEntry]:
        return self.remaining()



def reverse_manifest(plan: TargetPlan, config: ReAgentConfig, backend: REBackend,
                     llm: LLMProvider, session: Session, max_functions: int | None = None,
                     checker_llm: LLMProvider | None = None) -> list[ReversalResult]:
    return reverse_class(
        "", config, cast(REBackend, _ManifestBackend(backend, plan)), llm, session,
        max_functions, checker_llm, target_addresses={target.address for target in plan.functions},
    )
