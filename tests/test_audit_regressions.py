"""Regression coverage for the 0.3 audit, including real compiler gates."""

from __future__ import annotations

import json
import shutil
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from re_agent.agents.reverser import ReverserAgent
from re_agent.backend.exports import GhidraExportsBackend
from re_agent.backend.ghidra_bridge import GhidraBridgeBackend
from re_agent.backend.stub import StubBackend
from re_agent.config.loader import load_config
from re_agent.config.schema import ProjectProfile, ReAgentConfig, ValidationConfig
from re_agent.core.function_picker import pick_next
from re_agent.core.models import FunctionEntry, FunctionTarget, ReversalResult, Verdict, XRef
from re_agent.core.session import Session
from re_agent.llm.observed import CallBudget, ObservedProvider
from re_agent.orchestrator.single import reverse_single
from re_agent.parity.source_indexer import SourceIndexer
from re_agent.verification.candidate import cleanup_candidate_overlay, create_candidate_overlay, validate_candidate
from re_agent.verification.differential import compare_commands
from tests.test_agents.test_loop import MockLLM


def config_for(tmp_path: Path) -> ReAgentConfig:
    config = ReAgentConfig()
    config.project_profile.source_root = str(tmp_path / "src")
    Path(config.project_profile.source_root).mkdir()
    config.output.report_dir = str(tmp_path / "reports")
    config.output.log_dir = str(tmp_path / "logs")
    config.output.session_file = str(tmp_path / "session.json")
    config.orchestrator.objective_verifier_enabled = False
    config.orchestrator.investigation_enabled = False
    config.parity.enabled = False
    return config


def test_real_compiler_failure_is_repaired(tmp_path: Path) -> None:
    compiler = shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler required")
    config = config_for(tmp_path)
    source = Path(config.project_profile.source_root) / "f.cpp"
    source.write_text("int Foo() { return 0; }\nint main() { return Foo() == 7 ? 0 : 1; }\n")
    config.validation = ValidationConfig(
        build_commands=[f'"{compiler}" "{{candidate_file}}" -o "{{overlay_root}}/program"'],
        test_commands=['"{overlay_root}/program"'],
        trust_configured_commands=True,
    )
    reverser = MockLLM(["```cpp\nint Foo() { return missing; }\n```", "```cpp\nint Foo() { return 7; }\n```"])
    session = Session(config.output.session_file)
    result = reverse_single(
        FunctionTarget("0x100", "", "Foo"),
        config,
        StubBackend(),
        reverser,
        checker_llm=MockLLM(['{"verdict":"PASS"}']),
        session=session,
    )
    assert result.success and result.rounds_used == 2
    calls = sorted(Path(config.output.log_dir).glob("*/call-*-reverser.json"))
    assert len(calls) == 2
    assert "Candidate build gate failed" in calls[1].read_text()
    assert "return 0" in source.read_text()
    assert json.loads(session.previous_feedback("0x100"))["success"]


def test_real_runtime_failure_is_repaired(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    (Path(config.project_profile.source_root) / "f.cpp").write_text("int Foo() { return 0; }")
    # A deterministic project harness checks the candidate body, then reports
    # a concrete counterexample that must enter the next round's history.
    script = tmp_path / "check.py"
    script.write_text(
        "import pathlib,sys\ns=pathlib.Path(sys.argv[1]).read_text()\n"
        'print("counterexample: expected return 7")\nsys.exit(0 if "return 7" in s else 1)'
    )
    config.validation = ValidationConfig(
        runtime_commands=[f'"{sys.executable}" "{script}" "{{candidate_file}}"'],
        trust_configured_commands=True,
    )
    result = reverse_single(
        FunctionTarget("0x100", "", "Foo"),
        config,
        StubBackend(),
        MockLLM(["```cpp\nint Foo() { return 0; }\n```", "```cpp\nint Foo() { return 7; }\n```"]),
        checker_llm=MockLLM(['{"verdict":"PASS"}']),
    )
    assert result.success and result.rounds_used == 2


def test_bridge_preserves_known_symbol_and_fields() -> None:
    backend = GhidraBridgeBackend()
    with patch(
        "re_agent.backend.ghidra_bridge.run_cmd",
        return_value=(True, "// Known as: CTest::Foo\n// Signature: int Foo()\nint FUN_00401000() { return 1; }"),
    ):
        assert backend.decompile("0x401000").name == "CTest::Foo"
    with patch(
        "re_agent.backend.ghidra_bridge.run_cmd", return_value=(True, "// Size: 0x20 (32 bytes)\n// 0x10 value\n")
    ):
        struct = backend.get_struct("CTest")
        assert struct and struct.fields[0].offset == 16 and struct.fields[0].name == "value"
    assert backend._parse_xrefs("// no callees\n(no calls found)\nNote: export again") == []


def test_free_overload_is_rejected_before_any_replacement(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    file = Path(config.project_profile.source_root) / "f.cpp"
    text = "int Foo(int x) { return 1; }\nint Foo(double x) { return 2; }"
    file.write_text(text)
    config.validation.enabled = False
    result = reverse_single(
        FunctionTarget("0x100", "", "Foo"),
        config,
        StubBackend(),
        MockLLM(["```cpp\nint Foo(int x) { return 7; }\n```"]),
        checker_llm=MockLLM(['{"verdict":"PASS"}']),
    )
    assert not result.success
    assert result.validation_verdict and "Ambiguous" in result.validation_verdict.findings[0]
    assert file.read_text() == text


@pytest.mark.parametrize("absolute", [True, False])
def test_overlay_links_are_remapped(tmp_path: Path, absolute: bool) -> None:
    src = tmp_path / "src"
    src.mkdir()
    file = src / "a.cpp"
    file.write_text("int Foo() { return 1; }")
    (src / "z.cpp").symlink_to(file if absolute else Path("a.cpp"))
    source = SourceIndexer(src, ProjectProfile()).find("", "Foo")
    candidate = create_candidate_overlay(
        FunctionTarget("0x100", "", "Foo"),
        "int Foo() { return 2; }",
        source,
        src,
        tmp_path / "reports",
        project_root=tmp_path,
        copy_project=True,
    )
    try:
        (candidate.parent / "z.cpp").write_text("changed in scratch")
        assert file.read_text() == "int Foo() { return 1; }"
        assert candidate.read_text() == "changed in scratch"
    finally:
        cleanup_candidate_overlay(candidate)


def test_external_link_rejected(tmp_path: Path) -> None:
    project = tmp_path / "project"
    project.mkdir()
    external = tmp_path / "outside"
    external.write_text("unchanged")
    (project / "link").symlink_to(external)
    with pytest.raises(ValueError, match="symlink"):
        create_candidate_overlay(
            FunctionTarget("0x100", "", "Foo"),
            "int Foo() {}",
            None,
            project,
            tmp_path / "reports",
            project_root=project,
            copy_project=True,
        )
    assert external.read_text() == "unchanged"


@pytest.mark.parametrize("quote", ["", "'", '"'])
def test_shell_placeholders_are_data(tmp_path: Path, quote: str) -> None:
    file = tmp_path / "a'b $(touch injected).cpp"
    file.write_text("int f() {}")
    command = "test -f " + quote + "{candidate_file}" + quote
    result = validate_candidate(
        ValidationConfig(build_commands=[command], working_directory=str(tmp_path), trust_configured_commands=True),
        file,
        None,
    )
    assert result.verdict == Verdict.PASS
    assert not (tmp_path / "injected").exists()


def test_isolated_working_directory_cannot_escape(tmp_path: Path) -> None:
    file = tmp_path / "f.cpp"
    file.write_text("")
    with pytest.raises(ValueError, match="inside"):
        validate_candidate(
            ValidationConfig(copy_project=True, working_directory="..", build_commands=["true"]), file, str(file)
        )


def test_pending_actions_never_become_code() -> None:
    llm = MockLLM(['{"actions":[{"tool":"decompile","target":"0x100"}]}'])
    with pytest.raises(RuntimeError, match="budget exhausted"):
        ReverserAgent(llm, StubBackend(), max_investigations=1).reverse(FunctionTarget("0x100", "", "Foo"))


def test_shared_budget_and_failed_call_trace(tmp_path: Path) -> None:
    budget = CallBudget(1)
    first = ObservedProvider(MockLLM(["code"]), budget, "reverser", tmp_path)
    second = ObservedProvider(MockLLM(["PASS"]), budget, "checker", tmp_path)
    assert first.send([]) == "code"
    with pytest.raises(RuntimeError, match="budget exhausted"):
        second.send([])
    assert len(list(tmp_path.glob("call-*.json"))) == 1


def test_two_session_instances_do_not_lose_results(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    first, second = Session(path), Session(path)
    first.record_result(ReversalResult(FunctionTarget("0x100", "", "A"), "a"))
    second.record_result(ReversalResult(FunctionTarget("0x200", "", "B"), "b"))
    assert len(Session(path).get_all_functions()) == 2


def test_corrupt_session_not_silently_reset(tmp_path: Path) -> None:
    path = tmp_path / "session.json"
    path.write_text("{broken")
    with pytest.raises(ValueError):
        Session(path)
    assert path.read_text() == "{broken"


def test_dependency_order_uses_edges_not_degree(tmp_path: Path) -> None:
    class Backend(StubBackend):
        def xrefs_from(self, target: str) -> list[XRef]:
            return [XRef("0x200", "B", "CALL")] if target == "0x100" else []

    backend = Backend([FunctionEntry("0x100", "A", "C", 0), FunctionEntry("0x200", "B", "C", 100)])
    next_fn = pick_next("C", backend, Session(tmp_path / "state"), strategy="dependency-order")
    assert next_fn and next_fn.function_name == "B"


def test_failed_enumeration_is_not_empty_success(tmp_path: Path) -> None:
    class Broken(StubBackend):
        def remaining(self, class_name: str | None = None) -> list[FunctionEntry]:
            raise RuntimeError("missing exports")

        def unimplemented(self, filter_pattern: str | None = None) -> list[FunctionEntry]:
            raise RuntimeError("broken backend")

    with pytest.raises(RuntimeError, match="Cannot enumerate"):
        pick_next("C", Broken(), Session(tmp_path / "state"))


@pytest.mark.parametrize("value", ["-1", "0", "oops"])
def test_invalid_budget_rejected(tmp_path: Path, value: str) -> None:
    cfg = tmp_path / "config.yaml"
    cfg.write_text(f"orchestrator:\n  max_review_rounds: {value}\n")
    with pytest.raises(ValueError, match="positive integer"):
        load_config(cfg)


def test_differential_observes_return_and_memory(tmp_path: Path) -> None:
    good = [
        sys.executable,
        "-c",
        'import json,sys;x=json.load(sys.stdin);print(json.dumps({"return":x,"writes":[x+1]}))',
    ]
    bad = [sys.executable, "-c", 'import json,sys;x=json.load(sys.stdin);print(json.dumps({"return":x,"writes":[x]}))']
    result = compare_commands(good, bad, [0, 1, 9], tmp_path)
    assert not result.passed and result.cases_run == 1
    assert json.loads(result.findings[0])["case"] == 0
    assert compare_commands(good, good, [0, 1, 9], tmp_path).passed


@pytest.mark.parametrize("output", ["not json", "NaN", ""])
def test_differential_invalid_output_is_failure(tmp_path: Path, output: str) -> None:
    command = [sys.executable, "-c", f"print({output!r})"]
    assert not compare_commands(command, command, [1], tmp_path).passed


def test_json_backend_preserves_identity_and_rejects_bad_schema(tmp_path: Path) -> None:
    (tmp_path / "00401000.json").write_text(
        json.dumps(
            {
                "address": "00401000",
                "name": "FUN_00401000",
                "decompiled": "int f() { return 1; }",
                "signature": "int f()",
                "callers": [],
                "callees": [],
            }
        )
    )
    (tmp_path / "map.json").write_text(json.dumps({"401000": {"full_name": "CTest::Foo"}}))
    backend = GhidraExportsBackend(str(tmp_path), str(tmp_path / "map.json"))
    assert backend.decompile("0x401000").name == "CTest::Foo"
    assert backend.decompile("0x401000").callees == 0
    assert backend.get_pcode("0x401000") is None
    (tmp_path / "00401000.json").write_text('{"schema_version":999}')
    with pytest.raises(ValueError, match="schema"):
        backend.decompile("0x401000")


def test_clang_index_handles_namespaces_operators_and_utf8(tmp_path: Path) -> None:
    if not shutil.which("clang++"):
        pytest.skip("Clang is required for optional AST indexing")
    file = tmp_path / "f.cpp"
    file.write_text(
        "// éé\nnamespace app { struct C { int Foo(int); int Foo(double); int operator()(); }; }\n"
        "int app::C::Foo(int x) { return x; }\n"
        "int app::C::Foo(double x) { return int(x); }\n"
        "int app::C::operator()() { return 3; }\n"
    )
    database = tmp_path / "compile_commands.json"
    database.write_text(
        json.dumps(
            [{"directory": str(tmp_path), "file": str(file), "arguments": ["clang++", "-std=c++17", "-c", str(file)]}]
        )
    )
    index = SourceIndexer(tmp_path, ProjectProfile(compilation_database=str(database)))
    assert len(index.find_all("app::C", "Foo")) == 2
    assert index.find("app::C", "Foo") is None
    operator = index.find("app::C", "operator()")
    assert operator and operator.body == "{ return 3; }"
    assert file.read_text()[operator.body_start : operator.body_end] == operator.body


def test_class_candidates_compose_without_mutating_original(tmp_path: Path) -> None:
    from re_agent.orchestrator.class_runner import reverse_class

    compiler = shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler required")
    config = config_for(tmp_path)
    source = Path(config.project_profile.source_root) / "c.cpp"
    original = (
        "struct C { static int A(); static int B(); };\n"
        "int C::A() { return 0; }\nint C::B() { return 0; }\n"
        "int main() { return C::B()==0 ? (C::A()==7 ? 0 : 1) : (C::B()==8 ? 0 : 1); }\n"
    )
    source.write_text(original)
    config.validation = ValidationConfig(
        copy_project=True,
        project_root=str(tmp_path),
        build_commands=[f'"{compiler}" src/c.cpp -o program'],
        test_commands=["./program"],
        trust_configured_commands=True,
    )
    backend = StubBackend([FunctionEntry("0x100", "A", "C"), FunctionEntry("0x200", "B", "C")])
    results = reverse_class(
        "C",
        config,
        backend,
        MockLLM(["```cpp\nint C::A() { return 7; }\n```", "```cpp\nint C::B() { return A()+1; }\n```"]),
        checker_llm=MockLLM(['{"verdict":"PASS"}']),
        max_functions=2,
    )
    assert len(results) == 2 and all(r.success for r in results)
    assert source.read_text() == original


def test_checkpoint_diagnostics_reach_next_attempt(tmp_path: Path) -> None:
    session = Session(tmp_path / "state.json")
    previous = ReversalResult(
        FunctionTarget("0x100", "", "Foo"), "int Foo() { return missing; }", error="compile failed"
    )
    session.record_checkpoint(previous)

    class Inspect(MockLLM):
        def send(self, messages: list, **kwargs: object) -> str:
            assert "Previous attempt checkpoint" in messages[-1].content
            assert "return missing" in messages[-1].content
            return "```cpp\nint Foo() { return 7; }\n```"

    code, _ = ReverserAgent(Inspect([]), StubBackend(), session=session, investigation_enabled=False).reverse(
        previous.target
    )
    assert "return 7" in code


def test_log_ids_do_not_collide(tmp_path: Path) -> None:
    from re_agent.agents.loop import run_fix_loop

    for address in ["0x100", "0x200"]:
        run_fix_loop(
            FunctionTarget(address, "", "Foo"),
            StubBackend(),
            MockLLM(["```cpp\nint Foo() { return 7; }\n```"]),
            MockLLM(['{"verdict":"PASS"}']),
            log_dir=tmp_path,
            investigation_enabled=False,
        )
    assert len(list(tmp_path.glob("*/round1-result.json"))) == 2
    assert len(list(tmp_path.glob("*/call-*.json"))) == 4


def test_semantic_rule_enters_repair_loop(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    (Path(config.project_profile.source_root) / "f.cpp").write_text("int Foo() { return 0; }")
    rules = tmp_path / "rules.json"
    rules.write_text(json.dumps([{"id": "return-seven", "source_all_of": ["return 7"], "severity": "red"}]))
    config.parity.enabled = True
    config.parity.semantic_rules_file = str(rules)
    config.validation.enabled = False
    result = reverse_single(
        FunctionTarget("0x100", "", "Foo"),
        config,
        StubBackend(),
        MockLLM(["```cpp\nint Foo() { return 1; }\n```", "```cpp\nint Foo() { return 7; }\n```"]),
        checker_llm=MockLLM(['{"verdict":"PASS"}']),
    )
    assert result.success and result.rounds_used == 2


def test_estimate_counts_investigations_per_round(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from re_agent.cli.main import main

    path = tmp_path / "config.yaml"
    path.write_text("backend:\n  type: stub\norchestrator:\n  max_review_rounds: 4\n  max_investigations: 8\n")
    assert main(["--config", str(path), "estimate", "--address", "0x100"]) == 0
    assert "36 reverser + 4 checker" in capsys.readouterr().out


def test_doctor_flags_unusable_acceptance_policy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from re_agent.cli.main import main

    path = tmp_path / "config.yaml"
    path.write_text(f"backend:\n  type: stub\nproject_profile:\n  source_root: {tmp_path}\n")
    assert main(["--config", str(path), "doctor", "--address", "0x100"]) == 1
    report = json.loads(capsys.readouterr().out)
    assert any(c["check"] == "acceptance policy" and not c["passed"] for c in report["checks"])


def test_benchmark_reports_detected_mutation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from re_agent.cli.main import main

    manifest = tmp_path / "benchmark.json"
    manifest.write_text(
        json.dumps(
            [
                {
                    "name": "wrong return",
                    "reference": [sys.executable, "-c", "print(1)"],
                    "candidate": [sys.executable, "-c", "print(2)"],
                    "cases": [0],
                    "expected_match": False,
                }
            ]
        )
    )
    assert main(["benchmark", "--manifest", str(manifest)]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["expectations_met"] == 1 and not report["results"][0]["matched"]


def test_source_change_invalidates_completion_but_preserves_history(tmp_path: Path) -> None:
    from re_agent.core.identity import project_fingerprint

    config = config_for(tmp_path)
    file = Path(config.project_profile.source_root) / "f.cpp"
    file.write_text("old")
    session = Session(tmp_path / "state.json")
    session.bind(project_fingerprint(config))
    session.record_result(ReversalResult(FunctionTarget("0x100", "", "Foo"), "old", success=True))
    file.write_text("new")
    session.bind(project_fingerprint(config))
    assert not session.is_completed("0x100")
    assert json.loads(session.path.read_text())["history"][0]["functions"]["00000100"]["success"]


def test_timeout_kills_child_process(tmp_path: Path) -> None:
    import os
    import subprocess
    import time

    from re_agent.utils.process import run_process

    if os.name != "posix":
        pytest.skip("POSIX process group regression")
    marker = tmp_path / "child-survived"
    child = f'import time,pathlib;time.sleep(0.8);pathlib.Path({str(marker)!r}).write_text("alive")'
    parent = f'import subprocess,sys,time;subprocess.Popen([sys.executable,"-c",{child!r}]);time.sleep(10)'
    with pytest.raises(subprocess.TimeoutExpired):
        run_process([sys.executable, "-c", parent], timeout_s=0.2)
    time.sleep(0.9)
    assert not marker.exists()


def test_process_output_is_bounded() -> None:
    from re_agent.utils.process import run_process

    result = run_process([sys.executable, "-c", 'print("x"*100000)'], max_output_bytes=100)
    assert len(result.stdout) < 150 and result.stdout.startswith("[output truncated]")


def test_large_evidence_remains_parseable_json() -> None:
    from re_agent.utils.evidence import bounded_evidence

    raw = json.dumps({"nodes": [{"address": str(i), "details": "x" * 1000} for i in range(100)]})
    bounded = bounded_evidence(raw, 2000)
    assert len(bounded) <= 2000 and json.loads(bounded)["truncated"] is True


def test_differential_counterexample_repairs_compiled_candidate(tmp_path: Path) -> None:
    compiler = shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler required")
    config = config_for(tmp_path)
    source = Path(config.project_profile.source_root) / "f.cpp"
    source.write_text("#include <iostream>\nint Foo() { return 0; }\nint main() { std::cout << Foo(); }")
    cases = tmp_path / "cases.json"
    cases.write_text("[0,1]")
    config.validation = ValidationConfig(
        build_commands=[f'"{compiler}" "{{candidate_file}}" -o "{{overlay_root}}/program"'],
        differential_reference=[sys.executable, "-c", "print(7)"],
        differential_candidate=["{overlay_root}/program"],
        differential_cases_file=str(cases),
        trust_configured_commands=True,
    )
    result = reverse_single(
        FunctionTarget("0x100", "", "Foo"),
        config,
        StubBackend(),
        MockLLM(["```cpp\nint Foo() { return 1; }\n```", "```cpp\nint Foo() { return 7; }\n```"]),
        checker_llm=MockLLM(['{"verdict":"PASS"}']),
    )
    assert result.success and result.rounds_used == 2
    logs = sorted(Path(config.output.log_dir).glob("*/call-*-reverser.json"))
    assert "Candidate differential gate failed" in logs[1].read_text()
    assert result.validation_verdict and "All 2 differential cases matched" in result.validation_verdict.findings


def test_wrong_constant_return_is_not_structural_pass(tmp_path: Path) -> None:
    from re_agent.verification.objective import verify_candidate

    (tmp_path / "00000100.json").write_text(json.dumps({"address": "00000100", "decompiled": "int f() { return 1; }"}))
    backend = GhidraExportsBackend(str(tmp_path))
    assert (
        verify_candidate("int f() { return 999; }", FunctionTarget("0x100", "", "f"), backend).verdict == Verdict.FAIL
    )


def test_wrong_class_does_not_select_free_function(tmp_path: Path) -> None:
    (tmp_path / "f.cpp").write_text("int Foo() { return 1; }")
    assert SourceIndexer(tmp_path).find("MissingClass", "Foo") is None
