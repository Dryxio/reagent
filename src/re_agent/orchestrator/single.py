"""Single function reversal pipeline."""

from __future__ import annotations

import logging
from pathlib import Path

from re_agent.agents.loop import run_fix_loop
from re_agent.backend.protocol import REBackend
from re_agent.config.schema import ReAgentConfig
from re_agent.core.models import Finding, FunctionTarget, HookEntry, ReversalResult, ValidationVerdict, Verdict
from re_agent.core.session import Session
from re_agent.llm.protocol import LLMProvider
from re_agent.parity.engine import fetch_ghidra_data, score_single
from re_agent.parity.rules import read_semantic_rules
from re_agent.parity.source_indexer import SourceIndexer
from re_agent.verification.candidate import (
    _sanitize_path_component,
    cleanup_candidate_overlay,
    create_candidate_overlay,
    extract_candidate_body,
    validate_candidate,
)

logger = logging.getLogger(__name__)


def reverse_single(
    target: FunctionTarget,
    config: ReAgentConfig,
    backend: REBackend,
    llm: LLMProvider,
    session: Session | None = None,
    output_dir: Path | None = None,
    indexer: SourceIndexer | None = None,
    checker_llm: LLMProvider | None = None,
) -> ReversalResult:
    """Reverse a single function: agent loop -> optional parity check -> record.

    Args:
        output_dir: If provided, write the generated code to a file in this
            directory.  The file is named ``<address>_<class>_<func>.cpp``.
        indexer: Pre-built source indexer.  When running multiple functions
            in the same class, callers should build the indexer once and pass
            it here to avoid re-scanning the entire source tree each time.
    """
    log_dir = Path(config.output.log_dir) if config.output.log_dir else None

    def gate(result: ReversalResult) -> ReversalResult:
        return validate_result(result, config, backend, indexer)

    try:
        result = run_fix_loop(
            target=target,
            backend=backend,
            reverser_llm=llm,
            checker_llm=checker_llm or llm,
            max_rounds=config.orchestrator.max_review_rounds,
            log_dir=log_dir,
            source_root=Path(config.project_profile.source_root),
            project_profile=config.project_profile,
            indexer=indexer,
            session=session,
            report_dir=Path(config.output.report_dir),
            objective_verifier_enabled=config.orchestrator.objective_verifier_enabled,
            objective_call_count_tolerance=config.orchestrator.objective_call_count_tolerance,
            objective_control_flow_tolerance=config.orchestrator.objective_control_flow_tolerance,
            investigation_enabled=config.orchestrator.investigation_enabled,
            max_investigations=config.orchestrator.max_investigations,
            candidate_gate=gate,
            max_llm_calls=config.orchestrator.max_llm_calls_per_function,
        )
    except (RuntimeError, OSError, ValueError) as exc:
        result = ReversalResult(target=target, code="", success=False, error=str(exc))
        logger.error("Reversal failed for %s: %s", target.address, exc)

    # Write generated code to a file so users don't have to dig through logs
    if result.code:
        code_dir = output_dir or (Path(config.output.report_dir) / "code")
        try:
            code_dir.mkdir(parents=True, exist_ok=True)
            safe_name = f"{target.address}_{target.class_name}_{target.function_name}.cpp"
            safe_name = _sanitize_path_component(safe_name)
            code_path = code_dir / safe_name
            code_path.write_text(result.code, encoding="utf-8")
            logger.info("Code written to %s", code_path)
        except OSError as exc:
            logger.warning("Failed to write code file: %s", exc)

    if session:
        session.record_result(result)

    return result


def _target_to_hook(target: FunctionTarget) -> HookEntry:
    return HookEntry(
        class_path=target.class_name,
        fn_name=target.function_name,
        address=target.address,
        reversed=True,
        locked=False,
        is_virtual=False,
    )


def validate_result(
    result: ReversalResult,
    config: ReAgentConfig,
    backend: REBackend,
    indexer: SourceIndexer | None = None,
) -> ReversalResult:
    """Validate one round and return its candidate-level diagnostics."""
    target = result.target
    if result.code:
        candidate_file: Path | None = None
        try:
            if indexer is None:
                source_root = Path(config.project_profile.source_root)
                indexer = SourceIndexer(source_root, config.project_profile)
            source_root = Path(config.project_profile.source_root)
            matches = indexer.find_all(target.class_name, target.function_name)
            if len(matches) > 1:
                locations = ", ".join(f"{match.path}:{match.line}" for match in matches)
                raise ValueError(
                    f"Ambiguous overloaded source function; refusing to replace an arbitrary definition ({locations})"
                )
            original_source = indexer.find_by_address(target.address)
            if original_source is None:
                original_source = matches[0] if matches else indexer.find(target.class_name, target.function_name)
            candidate_file = create_candidate_overlay(
                target,
                result.code,
                original_source,
                source_root,
                Path(config.output.report_dir),
                project_root=Path(config.validation.project_root),
                copy_project=config.validation.copy_project,
            )
            candidate_body = extract_candidate_body(result.code)
            source = indexer.analyze_body(
                str(candidate_file),
                original_source.line if original_source else 1,
                candidate_body,
            )

            validation_verdict = validate_candidate(
                config.validation,
                candidate_file,
                original_source.path if original_source else None,
            )

            # Fetch Ghidra data from the backend for signal checks
            ghidra_data = None
            if config.parity.enabled and backend.capabilities.has_decompile:
                try:
                    ghidra_data = fetch_ghidra_data(target.address, backend)
                except Exception:
                    logger.debug("Ghidra data fetch failed for %s, running source-only", target.address, exc_info=True)

            status = None
            findings: list[Finding] = []
            if config.parity.enabled:
                status, findings = score_single(
                    entry=_target_to_hook(target),
                    source=source,
                    ghidra=ghidra_data,
                    config=config.parity,
                    semantic_rules=read_semantic_rules(Path(config.parity.semantic_rules_file))
                    if config.parity.semantic_rules_file
                    else None,
                )

            if not config.validation.enabled:
                validation_accepted = True
            elif config.validation.require_verified:
                validation_accepted = validation_verdict.verdict == Verdict.PASS
            else:
                validation_accepted = validation_verdict.verdict != Verdict.FAIL
            accepted = result.success and validation_accepted
            if status is not None:
                if config.validation.parity_fail_on_red and status.value == "red":
                    accepted = False
                if config.validation.parity_fail_on_yellow and status.value == "yellow":
                    accepted = False
            result = ReversalResult(
                target=result.target,
                code=result.code,
                checker_verdict=result.checker_verdict,
                objective_verdict=result.objective_verdict,
                validation_verdict=validation_verdict,
                parity_status=status,
                parity_findings=findings,
                rounds_used=result.rounds_used,
                success=accepted,
                run_id=result.run_id,
            )
        except (FileNotFoundError, OSError, ValueError) as exc:
            logger.warning("Candidate validation failed for %s: %s", target.address, exc)
            result.validation_verdict = ValidationVerdict(
                verdict=Verdict.FAIL,
                summary="Candidate overlay/validation failed",
                findings=[str(exc)],
            )
            result.success = False
        finally:
            if (
                candidate_file is not None
                and config.validation.copy_project
                and not config.validation.keep_project_copy
            ):
                cleanup_candidate_overlay(candidate_file)
                if result.validation_verdict is not None:
                    result.validation_verdict.overlay_file = None
                    result.validation_verdict.findings.append(
                        "Temporary isolated project copy removed after validation"
                    )

    return result
