"""Class-level auto-advance orchestrator."""

from __future__ import annotations

import copy
import json
import logging
import shutil
import sys
import tempfile
from pathlib import Path

from re_agent.backend.protocol import REBackend
from re_agent.config.schema import ReAgentConfig
from re_agent.core.function_picker import pick_next
from re_agent.core.models import ReversalResult
from re_agent.core.session import Session
from re_agent.llm.protocol import LLMProvider
from re_agent.orchestrator.parallel import ProviderFactory, reverse_parallel
from re_agent.orchestrator.single import reverse_single, validate_result
from re_agent.parity.source_indexer import SourceIndexer
from re_agent.verification.candidate import _remap_links, create_candidate_overlay

logger = logging.getLogger(__name__)


def _reverse_class(
    class_name: str,
    config: ReAgentConfig,
    backend: REBackend,
    llm: LLMProvider | None,
    session: Session | None = None,
    max_functions: int | None = None,
    checker_llm: LLMProvider | None = None,
) -> list[ReversalResult]:
    """Reverse functions in a class one by one until done or limit reached.

    Args:
        class_name: Target class name
        config: Full configuration
        backend: RE backend
        llm: LLM provider
        session: Session for progress tracking
        max_functions: Override for max functions (defaults to config value)

    Returns:
        List of ReversalResult for each attempted function
    """
    if llm is None:
        raise ValueError("Sequential execution requires a provider")
    if session is None:
        session = Session(config.output.session_file)

    limit = max_functions or config.orchestrator.max_functions_per_class
    results: list[ReversalResult] = []

    # Build the source indexer once for the entire class run.
    indexer: SourceIndexer | None = None
    source_root = Path(config.project_profile.source_root)
    if source_root.exists():
        indexer = SourceIndexer(source_root, config.project_profile)
    else:
        logger.warning("Source root %s not found, skipping index", source_root)

    for fn_idx in range(1, limit + 1):
        target = pick_next(
            class_name,
            backend,
            session,
            strategy=config.orchestrator.selection_strategy,
            max_attempts_per_function=config.orchestrator.max_attempts_per_function,
        )
        if target is None:
            print(f"No more candidates in {class_name}.", file=sys.stderr)
            break

        print(
            f"[{fn_idx}/{limit}] Reversing {target.class_name}::{target.function_name} ({target.address})...",
            file=sys.stderr,
        )

        result = reverse_single(
            target,
            config,
            backend,
            llm,
            checker_llm=checker_llm,
            session=session,
            indexer=indexer,
        )
        results.append(result)
        if result.success and config.validation.copy_project and config.orchestrator.cumulative_validation:
            _promote(result, config)
            indexer = SourceIndexer(Path(config.project_profile.source_root), config.project_profile)

        status = "PASS" if result.success else "FAIL"
        print(
            f"  -> {status} (rounds: {result.rounds_used})",
            file=sys.stderr,
        )

    return results


def _promote(result: ReversalResult, config: ReAgentConfig) -> None:
    source_root = Path(config.project_profile.source_root)
    indexer = SourceIndexer(source_root, config.project_profile)
    matches = indexer.find_all(result.target.class_name, result.target.function_name)
    if len(matches) != 1:
        raise ValueError("Cannot promote candidate without a unique source definition")
    candidate = create_candidate_overlay(
        result.target, result.code, matches[0], source_root, Path(config.output.report_dir)
    )
    Path(matches[0].path).write_text(candidate.read_text(encoding="utf-8"), encoding="utf-8")


def _reverse_class_run(
    class_name: str,
    config: ReAgentConfig,
    backend: REBackend,
    llm: LLMProvider | None,
    session: Session | None = None,
    max_functions: int | None = None,
    checker_llm: LLMProvider | None = None,
    *,
    target_addresses: set[str] | None = None,
    provider_factory: ProviderFactory | None = None,
) -> list[ReversalResult]:
    """Validate a class cumulatively in an isolated scratch project."""
    from re_agent.core.identity import project_fingerprint

    identity = project_fingerprint(config)
    parallel = config.orchestrator.max_parallel_functions > 1
    if parallel and provider_factory is None:
        raise ValueError("Parallel execution requires a provider_factory; live providers cannot be shared")

    def run(effective: ReAgentConfig, cumulative: bool = False) -> list[ReversalResult]:
        if not parallel:
            return _reverse_class(class_name, effective, backend, llm, session, max_functions, checker_llm)
        from re_agent.core.models import FunctionTarget

        try:
            entries = backend.remaining(class_name)
        except Exception:
            entries = []
        if not entries:
            entries = backend.unimplemented(class_name)
        reverse_rank = effective.orchestrator.selection_strategy == "high-impact"
        entries.sort(key=lambda f: (-f.caller_count if reverse_rank else f.caller_count, f.name, f.address))
        targets = [FunctionTarget(f.address, f.class_name or class_name, f.name, f.caller_count) for f in entries]

        def promote(result: ReversalResult, view: REBackend) -> ReversalResult:
            # Provisional successes must pass unchanged gates on the latest generation.
            result = validate_result(result, effective, view)
            if result.success:
                _promote(result, effective)
            return result

        assert provider_factory is not None
        return reverse_parallel(targets, effective, backend, session or Session(config.output.session_file),
                                provider_factory, max_functions or config.orchestrator.max_functions_per_class,
                                promote=promote if cumulative else None, identity=identity)

    if not (config.validation.copy_project and config.orchestrator.cumulative_validation):
        return run(config)
    original = Path(config.validation.project_root).resolve()
    relative_source = Path(config.project_profile.source_root).resolve().relative_to(original)
    session = session or Session(config.output.session_file)
    with tempfile.TemporaryDirectory(prefix="re-agent-class-") as directory:
        scratch = Path(directory)
        shutil.copytree(
            original,
            scratch,
            dirs_exist_ok=True,
            symlinks=True,
            ignore=shutil.ignore_patterns(".git", ".venv", "build", "reports", "__pycache__", "*.coordinator.lock"),
        )
        _remap_links(scratch, original)
        isolated = copy.deepcopy(config)
        isolated.validation.project_root = str(scratch)
        isolated.project_profile.source_root = str(scratch / relative_source)
        if config.project_profile.compilation_database:
            database = json.loads(Path(config.project_profile.compilation_database).read_text(encoding="utf-8"))
            remapped = json.dumps(database).replace(str(original), str(scratch))
            database_path = scratch / ".re-agent-compile_commands.json"
            database_path.write_text(remapped, encoding="utf-8")
            isolated.project_profile.compilation_database = str(database_path)
        isolated.output.report_dir = str(Path(config.output.report_dir).resolve())
        isolated.output.log_dir = str(Path(config.output.log_dir).resolve()) if config.output.log_dir else ""
        # Rebuild previously accepted functions before validating their dependents.
        from re_agent.core.models import FunctionTarget

        for entry in session.get_all_functions():
            from re_agent.utils.address import normalize_address

            selected = (normalize_address(entry["address"]) in target_addresses if target_addresses is not None
                        else entry.get("class_name") == class_name)
            if entry.get("success") and selected and entry.get("code"):
                _promote(
                    ReversalResult(
                        FunctionTarget(entry["address"], entry["class_name"], entry["function_name"]), entry["code"]
                    ),
                    isolated,
                )
        return run(isolated, cumulative=True)


def reverse_class(
    class_name: str, config: ReAgentConfig, backend: REBackend, llm: LLMProvider | None,
    session: Session | None = None, max_functions: int | None = None,
    checker_llm: LLMProvider | None = None, *, target_addresses: set[str] | None = None,
    provider_factory: ProviderFactory | None = None,
) -> list[ReversalResult]:
    if max_functions is not None and max_functions < 1:
        raise ValueError("Function attempt limit must be positive")
    session = session or Session(config.output.session_file)
    with session.coordinator():
        return _reverse_class_run(class_name, config, backend, llm, session, max_functions, checker_llm,
                                  target_addresses=target_addresses, provider_factory=provider_factory)
