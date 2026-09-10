"""Preserve actionable first compiler errors without unbounded repair prompts."""
import sys

from re_agent.config.schema import ValidationConfig
from re_agent.core.models import Verdict
from re_agent.verification.candidate import validate_candidate


def test_failed_command_preserves_first_error_and_final_summary(tmp_path):
    candidate = tmp_path / "candidate.cpp"
    candidate.write_text("void f() {}")
    script = (
        "import sys; "
        "print('error: missing declaration', file=sys.stderr); "
        "[print('cascade ' + str(i), file=sys.stderr) for i in range(40)]; "
        "print('compilation failed', file=sys.stderr); sys.exit(1)"
    )
    config = ValidationConfig(
        build_commands=[[sys.executable, "-c", script, "{candidate_file}"]],
        working_directory=str(tmp_path),
    )
    result = validate_candidate(config, candidate, None)
    detail = result.findings[-1]
    assert result.verdict == Verdict.FAIL
    assert "error: missing declaration" in detail
    assert "compilation failed" in detail
    assert "[intermediate output omitted]" in detail
    assert "cascade 20" not in detail


def test_stderr_precedes_stdout_and_long_diagnostic_lines_are_bounded(tmp_path):
    candidate = tmp_path / "candidate.cpp"
    candidate.write_text("void f() {}")
    script = "import sys; print('x'*20000); print('root error', file=sys.stderr); sys.exit(1)"
    config = ValidationConfig(build_commands=[[sys.executable, "-c", script, "{candidate_file}"]],
                              working_directory=str(tmp_path))
    result = validate_candidate(config, candidate, None)
    detail = result.findings[-1]
    assert "root error" in detail
    assert "x" * 501 not in detail
