"""Conservative structural verification that does not rely on an LLM."""

from __future__ import annotations

import json
import re

from re_agent.backend.protocol import REBackend
from re_agent.core.models import FunctionTarget, ObjectiveVerdict, Verdict
from re_agent.utils.text import count_calls, count_control_flow, strip_comments


def verify_candidate(
    code: str,
    target: FunctionTarget,
    backend: REBackend,
    call_count_tolerance: int = 3,
    control_flow_tolerance: int = 2,
) -> ObjectiveVerdict:
    """Return FAIL only on strong structural mismatches, else PASS/UNKNOWN."""
    if not code.strip():
        return ObjectiveVerdict(
            verdict=Verdict.FAIL,
            summary="No candidate code produced",
            findings=["Candidate code is empty"],
        )

    source_body = strip_comments(_extract_body(code))
    source_call_count, _, _ = count_calls(source_body)
    source_flow_count = count_control_flow(source_body)

    findings: list[str] = []
    checks_run = 0
    asm = None

    try:
        decompile = backend.decompile(target.address)
    except Exception as exc:
        return ObjectiveVerdict(
            verdict=Verdict.UNKNOWN,
            summary="Objective verifier could not read decompile output",
            findings=[str(exc)],
        )

    decompile_body = strip_comments(_extract_body(decompile.raw_output))
    constant_return = re.compile(r"\{\s*return\s+(-?(?:0x[0-9a-fA-F]+|[0-9]+))\s*;\s*\}")
    expected_constant = constant_return.fullmatch(decompile_body.strip())
    actual_constant = constant_return.fullmatch(source_body.strip())
    if expected_constant and actual_constant:
        checks_run += 1

        def integer(text: str) -> int:
            return int(text, 16) if "0x" in text.lower() else int(text, 10)

        if integer(expected_constant.group(1)) != integer(actual_constant.group(1)):
            findings.append("Constant return value differs from decompiled evidence")
    decompile_flow_count = count_control_flow(decompile_body)
    if decompile.callees is not None:
        checks_run += 1
        call_diff = abs(decompile.callees - source_call_count)
        if call_diff >= call_count_tolerance and source_call_count < decompile.callees:
            findings.append(
                f"Call count mismatch: decompile reports {decompile.callees} callees, "
                f"candidate has {source_call_count} calls"
            )

    if decompile_flow_count >= 2:
        checks_run += 1
        flow_diff = decompile_flow_count - source_flow_count
        if flow_diff >= control_flow_tolerance and source_flow_count < decompile_flow_count:
            findings.append(
                f"Control-flow mismatch: decompile has {decompile_flow_count} branches/loops, "
                f"candidate has {source_flow_count}"
            )

    if backend.capabilities.has_asm:
        try:
            asm = backend.get_asm(target.address)
        except Exception:
            asm = None
        if asm is not None:
            checks_run += 1
            call_diff = abs(asm.call_count - source_call_count)
            if call_diff >= call_count_tolerance and source_call_count < asm.call_count:
                findings.append(
                    f"ASM call mismatch: disassembly has {asm.call_count} calls, candidate has {source_call_count}"
                )

    if getattr(backend.capabilities, "has_cfg", False):
        cfg = _read_ir_artifact(backend, "get_cfg", target.address)
        if isinstance(cfg, list):
            cfg = [item for item in cfg if isinstance(item, dict) and "index" in item]
        if isinstance(cfg, list) and cfg:
            checks_run += 1
            # Source keywords do not predict machine basic-block counts: early
            # returns, short-circuit conditions and switches all change topology.
            # Only use CFG topology to flag wholesale removal of branching.
            branching_blocks = sum(
                isinstance(block.get("out"), list) and len(set(block["out"])) > 1
                for block in cfg
                if isinstance(block.get("out"), list)
                and all(isinstance(edge, int) for edge in block["out"])
            )
            source_has_branching = source_flow_count > 0 or bool(re.search(r"\?|&&|\|\|", source_body))
            if branching_blocks >= control_flow_tolerance and branching_blocks > 0 and not source_has_branching:
                findings.append(
                    f"CFG mismatch: binary has {branching_blocks} branching blocks, "
                    "candidate has no explicit control flow or conditional expressions"
                )

    if getattr(backend.capabilities, "has_pcode", False):
        pcode = _read_ir_artifact(backend, "get_pcode", target.address)
        if isinstance(pcode, list):
            pcode = [item for item in pcode if isinstance(item, dict) and item.get("opcode")]
        if isinstance(pcode, list) and pcode:
            if asm is not None:
                listed = set(re.findall(r"(?m)^\s*(?:0x)?([0-9a-fA-F]+)\s+", asm.instructions))
                asm_addresses = {int(address, 16) for address in listed}
                ir_addresses = {
                    int(str(item["address"]), 16) for item in pcode
                    if re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", str(item.get("address", "")))
                }
                outside = ir_addresses - asm_addresses
                if asm_addresses and outside:
                    return ObjectiveVerdict(
                        verdict=Verdict.FAIL,
                        summary="Structural evidence scopes differ; reconcile evidence before repairing code",
                        findings=[
                            f"P-code contains {len(outside)} instruction addresses absent from supplied assembly "
                            f"(first: {min(outside):x}). Expanded tail targets or incomplete exports may explain this; "
                            "cross-scope branch/call counts cannot establish a candidate mismatch."
                        ],
                        evidence_conflict=True,
                    )
            checks_run += 1
            opcodes = [str(item.get("opcode", "")).upper() for item in pcode if isinstance(item, dict)]
            ir_calls = sum(op in {"CALL", "CALLIND"} for op in opcodes)
            if ir_calls - source_call_count >= call_count_tolerance:
                findings.append(
                    f"P-code call mismatch: normalized IR has {ir_calls} calls, candidate has {source_call_count}"
                )
            ir_returns = sum(op == "RETURN" for op in opcodes)
            source_returns = source_body.count("return")
            if ir_returns >= 2 and source_returns == 0:
                findings.append(
                    f"P-code return mismatch: normalized IR has {ir_returns} returns, candidate has no explicit return"
                )

    if findings:
        return ObjectiveVerdict(
            verdict=Verdict.FAIL,
            summary="Objective verifier found structural mismatches",
            findings=findings,
        )
    if checks_run == 0:
        return ObjectiveVerdict(
            verdict=Verdict.UNKNOWN,
            summary="Objective verifier had insufficient structural data",
            findings=[],
        )
    return ObjectiveVerdict(
        verdict=Verdict.PASS,
        summary="No structural mismatches found",
        findings=[],
    )


def _extract_body(text: str) -> str:
    open_brace = text.find("{")
    close_brace = text.rfind("}")
    if open_brace == -1 or close_brace == -1 or close_brace <= open_brace:
        return text
    return text[open_brace : close_brace + 1]


def _read_ir_artifact(backend: REBackend, method_name: str, target: str) -> object | None:
    method = getattr(backend, method_name, None)
    if not callable(method):
        return None
    try:
        artifact = method(target)
        if artifact is None:
            return None
        payload = json.loads(artifact.content)
    except (AttributeError, json.JSONDecodeError, OSError, RuntimeError, TypeError, ValueError):
        return None
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list) and any(isinstance(item, dict) and "error" in item for item in data):
            return None
        return data
    return None
