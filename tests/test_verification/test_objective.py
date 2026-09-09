"""Regression tests for conservative structural evidence handling."""
from __future__ import annotations

import json

import pytest

from re_agent.backend.protocol import BackendCapabilities
from re_agent.core.models import AnalysisArtifact, DecompileResult, FunctionTarget, Verdict
from re_agent.verification.objective import verify_candidate


class _ErrorIRBackend:
    capabilities = BackendCapabilities(has_pcode=True, has_cfg=True)

    def decompile(self, target: str) -> DecompileResult:
        return DecompileResult(
            address=target,
            name="f",
            signature="void f()",
            decompiled="void f() {}",
            raw_output="void f() {}",
        )

    def get_pcode(self, target: str) -> AnalysisArtifact:
        payload = {"data": [{"error": "HighFunction unavailable"}]}
        return AnalysisArtifact("pcode", target, json.dumps(payload))

    def get_cfg(self, target: str) -> AnalysisArtifact:
        payload = {"data": [{"error": "CFG unavailable"}]}
        return AnalysisArtifact("cfg", target, json.dumps(payload))


def test_ir_error_objects_are_not_counted_as_successful_checks() -> None:
    verdict = verify_candidate(
        "void f() {}",
        FunctionTarget("0x100", "", "f"),
        _ErrorIRBackend(),  # type: ignore[arg-type]
    )
    assert verdict.verdict == Verdict.UNKNOWN


class _CFGBackend:
    capabilities = BackendCapabilities(has_cfg=True)

    def __init__(self, source: str, blocks: list[dict]) -> None:
        self.source = source
        self.blocks = blocks

    def decompile(self, target: str) -> DecompileResult:
        return DecompileResult(address=target, name="f", signature="int f(int x)",
                               decompiled=self.source, raw_output=self.source)

    def get_cfg(self, target: str) -> AnalysisArtifact:
        return AnalysisArtifact("cfg", target, json.dumps({"data": self.blocks}))


EARLY_RETURN_SOURCE = "int f(int x) { if (x == 0) return 10; if (x == 1) return 20; if (x == 2) return 30; return 0; }"
EARLY_RETURN_CFG = [
    {"index": 0, "out": [1, 2]}, {"index": 1, "out": []},
    {"index": 2, "out": [3, 4]}, {"index": 3, "out": []},
    {"index": 4, "out": [5, 6]}, {"index": 5, "out": []}, {"index": 6, "out": []},
]


@pytest.mark.parametrize("source", [
    EARLY_RETURN_SOURCE,
    "int f(int x) { return x == 0 ? 10 : x == 1 ? 20 : x == 2 ? 30 : 0; }",
    "int f(int x) { switch (x) { case 0: return 10; case 1: return 20; case 2: return 30; default: return 0; } }",
    "int f(int x) { return x > 0 && x < 10 && x != 5; }",
])
def test_cfg_does_not_estimate_block_count_from_source_keywords(source: str) -> None:
    result = verify_candidate(source, FunctionTarget("100", "", "f"),
                              _CFGBackend(source, EARLY_RETURN_CFG))  # type: ignore[arg-type]
    assert result.verdict == Verdict.PASS


def test_cfg_still_rejects_removed_branching_without_decompiler_support() -> None:
    # The CFG can catch a trivial replacement even if the decompiler omits flow.
    result = verify_candidate("int f(int x) { return x; }", FunctionTarget("100", "", "f"),
                              _CFGBackend("unavailable", EARLY_RETURN_CFG))  # type: ignore[arg-type]
    assert result.verdict == Verdict.FAIL
    assert any("CFG mismatch" in finding for finding in result.findings)


def test_removed_branches_still_fail_decompile_check() -> None:
    result = verify_candidate("int f(int x) { return 0; }", FunctionTarget("100", "", "f"),
                              _CFGBackend(EARLY_RETURN_SOURCE, EARLY_RETURN_CFG))  # type: ignore[arg-type]
    assert result.verdict == Verdict.FAIL
    assert any("Control-flow mismatch" in finding for finding in result.findings)


def test_linear_cfg_blocks_do_not_imply_branching() -> None:
    blocks = [{"index": i, "out": [i + 1] if i < 7 else []} for i in range(8)]
    result = verify_candidate("int f(int x) { return x; }", FunctionTarget("100", "", "f"),
                              _CFGBackend("int f(int x) { return x; }", blocks))  # type: ignore[arg-type]
    assert result.verdict == Verdict.PASS
