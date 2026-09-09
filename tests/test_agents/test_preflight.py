"""Reject unusable candidate shapes before spending reviewer calls."""
import json

import pytest

from re_agent.agents.loop import run_fix_loop
from re_agent.backend.stub import StubBackend
from re_agent.core.models import FunctionTarget
from tests.test_agents.test_loop import MockLLM


@pytest.mark.parametrize("invalid", [
    "struct Layout { int field; }; int f(Layout* p) { return p->field; }",
    "int f() { return 0;",
    "int f() { return 0; } int g() { return 1; }",
])
def test_invalid_candidate_skips_reviewer_then_repairs_within_budget(tmp_path, invalid):
    reverser = MockLLM([f"```cpp\n{invalid}\n```", "```cpp\nint f() { return 0; }\n```"])
    checker = MockLLM(['{"verdict":"PASS","summary":"Matches","issues":[],"fix_instructions":[]}'])
    result = run_fix_loop(FunctionTarget("100", "", "f"), StubBackend(), reverser, checker,
                          candidate_gate=lambda result: result, objective_verifier_enabled=False,
                          max_rounds=2, max_llm_calls=3, log_dir=tmp_path)
    assert result.success
    assert result.rounds_used == 2
    assert reverser._idx == 2
    assert checker._idx == 1
    log = next(tmp_path.glob("*/round1-*-checker.json"))
    assert json.loads(log.read_text())["checker_skipped"] is True


def test_repeated_invalid_candidates_never_call_reviewer(tmp_path):
    reverser = MockLLM(["```cpp\nint f() {\n```"])
    checker = MockLLM(["VERDICT: PASS"])
    result = run_fix_loop(FunctionTarget("100", "", "f"), StubBackend(), reverser, checker,
                          candidate_gate=lambda result: result, objective_verifier_enabled=False,
                          max_rounds=3, log_dir=tmp_path)
    assert not result.success
    assert checker._idx == 0
    assert result.error and "without progress" in result.error
