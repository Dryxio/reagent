"""Real compiler failures feed repair before spending reviewer calls."""
import shutil

import pytest

from re_agent.backend.stub import StubBackend
from re_agent.config.schema import ReAgentConfig
from re_agent.core.models import FunctionTarget, Verdict
from re_agent.orchestrator.single import reverse_single
from tests.test_agents.test_loop import MockLLM


def test_compile_failure_repairs_without_reviewing_or_rebuilding_bad_output(tmp_path, monkeypatch):
    compiler = shutil.which("clang++") or shutil.which("g++")
    if not compiler:
        pytest.skip("C++ compiler unavailable")
    config = ReAgentConfig()
    source_root = tmp_path / "src"
    source_root.mkdir()
    config.project_profile.source_root = str(source_root)
    config.output.report_dir = str(tmp_path / "reports")
    config.output.log_dir = str(tmp_path / "logs")
    config.parity.enabled = False
    config.orchestrator.objective_verifier_enabled = False
    config.orchestrator.max_review_rounds = 2
    config.orchestrator.max_llm_calls_per_function = 3
    config.validation.build_commands = [[compiler, "-std=c++20", "-fsyntax-only", "{candidate_file}"]]
    config.validation.require_build = True
    config.validation.trust_configured_commands = True
    config.validation.working_directory = str(tmp_path)
    bad = 'std::uint32_t f() { return 1; }'
    good = '#include <cstdint>\nstd::uint32_t f() { return 1; }'
    reverser = MockLLM([f"```cpp\n{bad}\n```", f"```cpp\n{good}\n```"])
    checker = MockLLM(['{"verdict":"PASS","summary":"Matches"}'])
    from re_agent.verification import candidate

    original = candidate.run_process
    calls = []

    def observed(*args, **kwargs):
        calls.append(args)
        return original(*args, **kwargs)

    monkeypatch.setattr(candidate, "run_process", observed)
    result = reverse_single(FunctionTarget("100", "", "f"), config, StubBackend(), reverser, checker_llm=checker)
    assert result.success
    assert result.rounds_used == 2
    assert checker._idx == 1
    assert reverser._idx == 2
    assert len(calls) == 2  # Exactly once per candidate, never again after review.
    assert result.validation_verdict.verdict == Verdict.PASS


def test_successful_local_gate_does_not_override_failed_model_review(tmp_path):
    config = ReAgentConfig()
    source_root = tmp_path / "src"
    source_root.mkdir()
    config.project_profile.source_root = str(source_root)
    config.output.report_dir = str(tmp_path / "reports")
    config.output.log_dir = str(tmp_path / "logs")
    config.parity.enabled = False
    config.orchestrator.max_review_rounds = 1
    config.validation.require_verified = False
    result = reverse_single(FunctionTarget("100", "", "f"), config, StubBackend(),
                            MockLLM(['```cpp\nint f() { return 1; }\n```']),
                            checker_llm=MockLLM(['{"verdict":"FAIL","summary":"Wrong return"}']))
    assert not result.success
    assert result.checker_verdict.verdict == Verdict.FAIL

