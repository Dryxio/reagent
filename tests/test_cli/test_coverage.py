import json
import sys
from pathlib import Path

from re_agent.config.schema import ValidationConfig
from re_agent.core.models import FunctionTarget, ReversalResult, ValidationVerdict, Verdict
from re_agent.core.session import Session
from re_agent.core.target_plan import TargetPlan
from re_agent.reports.coverage import manifest_coverage
from re_agent.verification.candidate import validate_candidate


def test_coverage_accounts_for_inventory_without_mutating_session(tmp_path: Path) -> None:
    targets = [FunctionTarget(f"00000{i}00", "", f"f{i}") for i in range(1, 5)]
    plan = TargetPlan("a" * 64, [targets[0].address], targets)
    session = Session(tmp_path / "session.json")
    session.bind(plan.identity)
    session.record_result(ReversalResult(targets[0], "", success=True,
                                        validation_verdict=ValidationVerdict(Verdict.UNKNOWN, "disabled")))
    session.record_result(ReversalResult(targets[1], "", success=False))
    session.record_checkpoint(ReversalResult(targets[2], "candidate"))
    session.record_result(ReversalResult(FunctionTarget("999", "", "outside"), "", success=True))
    before = session.path.read_bytes()
    report = manifest_coverage(plan, session, plan.identity)
    assert report["totals"] == {"unattempted": 1, "attempted": 1, "accepted": 1, "failed": 1, "stale": 0}
    assert sum(report["totals"].values()) == report["planned"] == 4
    assert report["functions"][0]["candidate_validation"] == "UNKNOWN"
    stale = manifest_coverage(plan, session, "b" * 64)
    assert stale["totals"]["stale"] == 3
    assert stale["totals"]["unattempted"] == 1
    assert session.path.read_bytes() == before


def test_gate_outcomes_survive_session_roundtrip(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.cpp"
    candidate.write_text("int f() {}", encoding="utf-8")
    verdict = validate_candidate(ValidationConfig(
        build_commands=[[sys.executable, "-c", "pass", "{candidate_file}"]],
        test_commands=[[sys.executable, "-c", "raise SystemExit(4)", "{candidate_file}"]],
        runtime_commands=[[sys.executable, "-c", "pass", "{candidate_file}"]],
    ), candidate, None)
    assert verdict.verdict == Verdict.FAIL
    assert [(check["kind"], check["verdict"]) for check in verdict.checks] == [("build", "PASS"), ("test", "FAIL")]
    session = Session(tmp_path / "session.json")
    session.record_result(ReversalResult(FunctionTarget("100", "", "f"), "", validation_verdict=verdict))
    assert Session(session.path).get_all_functions()[0]["validation_checks"] == verdict.checks


def test_status_cli_preserves_stale_session(tmp_path: Path, capsys) -> None:
    from re_agent.cli.main import main
    config = tmp_path / "config.json"
    session = Session(tmp_path / "session.json")
    session.bind("a" * 64)
    plan = TargetPlan("a" * 64, ["00000100"], [FunctionTarget("00000100", "", "f")])
    plan.save(tmp_path / "plan.json")
    config.write_text(json.dumps({"output": {"session_file": str(session.path)}}))
    before = session.path.read_bytes()
    assert main(["--config", str(config), "status", "--manifest", str(tmp_path / "plan.json"), "--format", "json"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["manifest_current"] is False
    assert session.path.read_bytes() == before
