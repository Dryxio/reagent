"""Output formatters for re-agent results."""

from __future__ import annotations

import json
from typing import Any

from re_agent.core.models import ReversalResult


def format_result(result: ReversalResult, include_code: bool = True) -> str:
    """Format a single result for terminal display."""
    status = "PASS" if result.success else "FAIL"
    lines = [
        f"{result.target.class_name}::{result.target.function_name} ({result.target.address})",
        f"  Status: {status} | Rounds: {result.rounds_used}",
    ]
    if result.error:
        lines.append(f"  Error: {result.error}")
    if result.checker_verdict:
        lines.append(f"  Verdict: {result.checker_verdict.verdict.value}")
        if result.checker_verdict.summary:
            lines.append(f"  Summary: {result.checker_verdict.summary}")
        if result.checker_verdict.issues:
            lines.append("  Issues:")
            for issue in result.checker_verdict.issues:
                lines.append(f"    - {issue}")
    if result.objective_verdict:
        lines.append(f"  Objective: {result.objective_verdict.verdict.value}")
        if result.objective_verdict.summary:
            lines.append(f"  Objective Summary: {result.objective_verdict.summary}")
        if result.objective_verdict.findings:
            lines.append("  Objective Findings:")
            for finding in result.objective_verdict.findings:
                lines.append(f"    - {finding}")
    if result.validation_verdict:
        lines.append(f"  Candidate validation: {result.validation_verdict.verdict.value}")
        lines.append(f"  Validation summary: {result.validation_verdict.summary}")
        if result.validation_verdict.overlay_file:
            lines.append(f"  Candidate overlay: {result.validation_verdict.overlay_file}")
    if result.parity_status:
        lines.append(f"  Parity: {result.parity_status.value}")
    if result.parity_findings:
        for f in result.parity_findings:
            lines.append(f"    [{f.level}] {f.reason}")
    if include_code and result.code:
        lines.append("  Code:")
        lines.append("  ```cpp")
        for code_line in result.code.splitlines():
            lines.append(f"  {code_line}")
        lines.append("  ```")
    return "\n".join(lines)


def results_to_json(results: list[ReversalResult]) -> str:
    """Serialize results list to JSON string."""
    data = [_result_to_dict(r) for r in results]
    return json.dumps({"results": data}, indent=2)


def results_to_markdown(results: list[ReversalResult]) -> str:
    """Format results as a markdown table."""
    lines = [
        "| Address | Function | Status | Rounds | Parity |",
        "|---------|----------|--------|--------|--------|",
    ]
    for r in results:
        status = "PASS" if r.success else "FAIL"
        parity = r.parity_status.value if r.parity_status else "-"
        fn = f"{r.target.class_name}::{r.target.function_name}"
        lines.append(f"| {r.target.address} | {fn} | {status} | {r.rounds_used} | {parity} |")
    return "\n".join(lines)


def _result_to_dict(result: ReversalResult) -> dict[str, Any]:
    d: dict[str, Any] = {
        "address": result.target.address,
        "run_id": result.run_id,
        "error": result.error,
        "class_name": result.target.class_name,
        "function_name": result.target.function_name,
        "success": result.success,
        "rounds_used": result.rounds_used,
        "code": result.code if result.code else None,
    }
    if result.checker_verdict:
        d["verdict"] = result.checker_verdict.verdict.value
        d["summary"] = result.checker_verdict.summary
        d["issues"] = result.checker_verdict.issues
    if result.objective_verdict:
        d["objective_verdict"] = result.objective_verdict.verdict.value
        d["objective_summary"] = result.objective_verdict.summary
        d["objective_findings"] = result.objective_verdict.findings
    if result.validation_verdict:
        d["validation_verdict"] = result.validation_verdict.verdict.value
        d["validation_summary"] = result.validation_verdict.summary
        d["validation_findings"] = result.validation_verdict.findings
        d["validation_checks"] = result.validation_verdict.checks
        d["candidate_overlay"] = result.validation_verdict.overlay_file
    if result.parity_status:
        d["parity_status"] = result.parity_status.value
    if result.parity_findings:
        d["parity_findings"] = [{"level": f.level, "reason": f.reason} for f in result.parity_findings]
    return d
