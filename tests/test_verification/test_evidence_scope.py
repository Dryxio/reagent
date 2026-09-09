"""Expanded or incomplete IR must not drive repeated candidate repairs."""
import json

from re_agent.agents.loop import run_fix_loop
from re_agent.backend.protocol import BackendCapabilities
from re_agent.backend.stub import StubBackend
from re_agent.core.models import AnalysisArtifact, AsmResult, FunctionTarget, Verdict
from re_agent.verification.objective import verify_candidate
from tests.test_agents.test_loop import MockLLM


class ScopeBackend(StubBackend):
    def __init__(self, address="200"):
        self.ir_address = address

    @property
    def capabilities(self):
        return BackendCapabilities(has_asm=True, has_pcode=True)

    def get_asm(self, address):
        return AsmResult(address, "100 CALL RAX\n102 JMP RBX", 2, 1, False)

    def get_pcode(self, address):
        return AnalysisArtifact("pcode", address, json.dumps({"data": [
            {"address": self.ir_address, "opcode": "CALLIND"}]}))


def test_out_of_scope_ir_is_reported_as_evidence_conflict():
    result = verify_candidate("void f() {}", FunctionTarget("100", "", "f"), ScopeBackend())
    assert result.verdict == Verdict.FAIL
    assert result.evidence_conflict
    assert "200" in result.findings[0]
    assert "candidate mismatch" in result.findings[0]


def test_matching_ir_scope_does_not_block_candidate():
    result = verify_candidate("void f() {}", FunctionTarget("100", "", "f"), ScopeBackend("0x100"))
    assert not result.evidence_conflict


def test_evidence_conflict_stops_repair_loop_without_accepting_candidate():
    reverser = MockLLM(["```cpp\nvoid f() {}\n```"])
    checker = MockLLM(['{"verdict":"PASS","summary":"Matches assembly"}'])
    result = run_fix_loop(FunctionTarget("100", "", "f"), ScopeBackend(), reverser, checker, max_rounds=3)
    assert not result.success
    assert result.error == "Stopped: reconcile incompatible structural evidence before retrying"
    assert result.rounds_used == 1
    assert reverser._idx == checker._idx == 1
