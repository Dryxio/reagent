"""Portable gates execute real processes without shell interpretation."""
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from re_agent.config.loader import load_config
from re_agent.config.schema import ValidationConfig
from re_agent.core.models import Verdict
from re_agent.verification.candidate import validate_candidate


def test_argument_data_and_candidate_path(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate $special {overlay_root}.cpp"
    candidate.write_text("candidate", encoding="utf-8")
    literal = "$(echo unsafe); & `literal`"
    script = "import sys,pathlib; assert pathlib.Path(sys.argv[1]).read_text() == 'candidate'; "
    script += "assert sys.argv[2] == " + repr(literal)
    config = ValidationConfig(build_commands=[[sys.executable, "-c", script, "{candidate_file}", literal]],
                              working_directory=str(tmp_path), trust_configured_commands=True)
    assert validate_candidate(config, candidate, None).verdict == Verdict.PASS


@pytest.mark.parametrize("script, message", [("import sys;sys.exit(7)", "failed"),
                                            ("import time;time.sleep(10)", "timed out")])
def test_argument_gate_failures(tmp_path: Path, script: str, message: str) -> None:
    candidate = tmp_path / "candidate.cpp"
    config = ValidationConfig(build_commands=[[sys.executable, "-c", script, "{candidate_file}"]],
                              command_timeout_s=1, working_directory=str(tmp_path))
    result = validate_candidate(config, candidate, None)
    assert result.verdict == Verdict.FAIL
    assert message in result.summary


def test_missing_shell_returns_failure(tmp_path: Path) -> None:
    with patch("re_agent.verification.candidate.run_process", side_effect=FileNotFoundError("/bin/sh")):
        result = validate_candidate(ValidationConfig(build_commands=["test -f {candidate_file}"]),
                                    tmp_path / "candidate.cpp", None)
    assert result.verdict == Verdict.FAIL
    assert "could not start" in result.summary


@pytest.mark.parametrize("command", [[], [123], [""], "", {"args": ["python"]}])
def test_invalid_commands_rejected(tmp_path: Path, command: object) -> None:
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"validation": {"build_commands": [command]}}), encoding="utf-8")
    with pytest.raises(ValueError, match="argument arrays"):
        load_config(path)


def test_argument_yaml_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text('validation:\n  build_commands:\n    - [python, check.py, "{candidate_file}"]\n', encoding="utf-8")
    assert load_config(path).validation.build_commands == [["python", "check.py", "{candidate_file}"]]


def test_doctor_reports_shell_requirement_only_for_strings(tmp_path: Path, capsys) -> None:
    from re_agent.cli.main import main

    path = tmp_path / "config.json"
    config = {"backend": {"type": "stub"}, "project_profile": {"source_root": str(tmp_path)},
              "validation": {"trust_configured_commands": True, "build_commands": [[sys.executable, "-c", "pass"]]}}
    path.write_text(json.dumps(config), encoding="utf-8")
    with patch("re_agent.cli.cmd_doctor.shutil.which", return_value=None):
        assert main(["--config", str(path), "doctor"]) == 0
        capsys.readouterr()
        config["validation"]["build_commands"] = ["true"]
        path.write_text(json.dumps(config), encoding="utf-8")
        assert main(["--config", str(path), "doctor"]) == 1
        report = json.loads(capsys.readouterr().out)
        assert any(check["check"] == "validation shell" and not check["passed"] for check in report["checks"])
