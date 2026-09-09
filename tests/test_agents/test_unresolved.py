"""Do not accept unresolved decompiler targets as reconstructed control flow."""
import pytest

from re_agent.agents.loop import run_fix_loop
from re_agent.agents.reverser import ReverserAgent
from re_agent.backend.protocol import BackendCapabilities
from re_agent.backend.stub import StubBackend
from re_agent.core.models import AsmResult, FunctionTarget
from re_agent.verification.candidate import unresolved_placeholders
from tests.test_agents.test_loop import MockLLM


@pytest.mark.parametrize("code", [
    'void f() { UNRECOVERED_JUMPTABLE(); }',
    'extern void UNRECOVERED_JUMPTABLE(); void f() { UNRECOVERED_JUMPTABLE(); }',
    'void f() { ((void(*)())UNRECOVERED_JUMPTABLE_1)(); }',
])
def test_placeholder_cannot_pass_even_without_validation_gate(code):
    checker = MockLLM(['{"verdict":"PASS","summary":"ok"}'])
    result = run_fix_loop(FunctionTarget("100", "", "f"), StubBackend(),
                          MockLLM([f"```cpp\n{code}\n```"]), checker, max_rounds=1)
    assert not result.success
    assert checker._idx == 0
    assert "Unresolved" in result.checker_verdict.issues[0]


def test_comments_and_literals_do_not_trigger_placeholder_detection():
    code = '''// UNRECOVERED_JUMPTABLE
    void f() { /* UNRECOVERED_JUMPTABLE */
      const char* a = "UNRECOVERED_JUMPTABLE";
      const char* b = R"tag(escaped " UNRECOVERED_JUMPTABLE)tag";
    }'''
    assert unresolved_placeholders(code) == []


def test_disassembly_is_available_with_small_evidence_budget():
    class Backend(StubBackend):
        @property
        def capabilities(self):
            return BackendCapabilities(has_asm=True)

        def get_asm(self, address):
            return AsmResult(address, "100 JMP RAX", 1, 0, False)

    agent = ReverserAgent(MockLLM([]), Backend(), max_investigations=1)
    assert "100 JMP RAX" in agent._build_investigation_context(FunctionTarget("100", "", "f"))
