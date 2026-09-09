import json
from pathlib import Path
from unittest.mock import patch

import pytest

from re_agent.backend.stub import StubBackend
from re_agent.cli.main import main
from re_agent.core.identity import project_fingerprint
from re_agent.core.models import FunctionTarget, ReversalResult, XRef
from re_agent.core.session import Session
from re_agent.core.target_plan import TargetPlan
from re_agent.orchestrator.batch_runner import reverse_manifest
from tests.test_agents.test_loop import MockLLM
from tests.test_audit_regressions import config_for


def test_manifest_orders_mixed_classes_and_resumes(tmp_path: Path) -> None:
    class Backend(StubBackend):
        def xrefs_from(self, target: str) -> list[XRef]:
            return [XRef("200", "B", "CALL"), XRef("999", "External", "CALL")] if target == "00000100" else []
    config = config_for(tmp_path)
    session = Session(config.output.session_file)
    plan = TargetPlan("a" * 64, ["00000100"],
                      [FunctionTarget("00000100", "A", "Caller"), FunctionTarget("00000200", "B", "Leaf")])
    visited = []

    def reverse(target, *args, **kwargs):
        visited.append(target.address)
        result = ReversalResult(target, "int f() {}", success=True)
        kwargs["session"].record_result(result)
        return result

    with patch("re_agent.orchestrator.class_runner.reverse_single", side_effect=reverse):
        first = reverse_manifest(plan, config, Backend(), MockLLM([]), session, max_functions=1)
        second = reverse_manifest(plan, config, Backend(), MockLLM([]), session, max_functions=2)
    assert len(first) == len(second) == 1
    assert visited == ["00000200", "00000100"]
    assert not session.is_attempted("999")


def test_manifest_failed_attempt_limit_is_preserved(tmp_path: Path) -> None:
    config = config_for(tmp_path)
    config.orchestrator.max_attempts_per_function = 1
    session = Session(config.output.session_file)
    target = FunctionTarget("00000100", "", "f")
    session.record_result(ReversalResult(target, "", success=False))
    plan = TargetPlan("a" * 64, [target.address], [target])
    with patch("re_agent.orchestrator.class_runner.reverse_single", side_effect=AssertionError("must not retry")):
        assert reverse_manifest(plan, config, StubBackend(), MockLLM([]), session) == []


def test_manifest_preflight_before_provider(tmp_path: Path) -> None:
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"backend": {"type": "stub"}, "validation": {"enabled": False}}))
    from re_agent.config.loader import load_config
    plan = TargetPlan(project_fingerprint(load_config(config_path)), ["00000100"],
                      [FunctionTarget("00000100", "", "f")])
    path = tmp_path / "plan.json"
    plan.save(path)
    with patch("re_agent.llm.registry.create_provider", side_effect=AssertionError("must not call")):
        assert main(["--config", str(config_path), "reverse", "--manifest", str(path), "--dry-run"]) == 0
        plan.identity = "a" * 64
        plan.save(path)
        assert main(["--config", str(config_path), "reverse", "--manifest", str(path)]) == 1


@pytest.mark.parametrize("flag", ["--address", "--class"])
def test_manifest_excludes_other_target_modes(flag: str) -> None:
    assert main(["reverse", "--manifest", "missing.json", flag, "100"]) == 1


def test_manifest_cumulative_resume_across_classes(tmp_path: Path) -> None:
    import shutil

    from re_agent.config.schema import ValidationConfig

    compiler = shutil.which("c++")
    if not compiler:
        pytest.skip("C++ compiler required")
    config = config_for(tmp_path)
    file = Path(config.project_profile.source_root) / "group.cpp"
    original = ("struct A { static int f(); }; struct B { static int g(); };\n"
                "int A::f() { return 0; }\nint B::g() { return 0; }\n"
                "int main() { return A::f()==7 && (B::g()==0 || B::g()==8) ? 0 : 1; }")
    file.write_text(original, encoding="utf-8")
    config.validation = ValidationConfig(
        copy_project=True, project_root=str(tmp_path), trust_configured_commands=True,
        build_commands=[[compiler, "src/group.cpp", "-o", "program.exe"]],
        test_commands=[["{overlay_root}/program.exe"]],
    )
    plan = TargetPlan("a" * 64, ["00000100"],
                      [FunctionTarget("00000100", "A", "f"), FunctionTarget("00000200", "B", "g")])
    session = Session(config.output.session_file)
    first = reverse_manifest(plan, config, StubBackend(),
                             MockLLM(["```cpp\nint A::f() { return 7; }\n```"]), session, 1,
                             MockLLM(['{"verdict":"PASS"}']))
    second = reverse_manifest(plan, config, StubBackend(),
                              MockLLM(["```cpp\nint B::g() { return A::f()+1; }\n```"]), session, 1,
                              MockLLM(['{"verdict":"PASS"}']))
    assert first[0].success and second[0].success
    assert file.read_text(encoding="utf-8") == original
