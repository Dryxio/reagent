"""Smoke tests for CLI."""
from __future__ import annotations

from pathlib import Path

from re_agent.cli.main import build_parser, main


def test_parser_builds() -> None:
    parser = build_parser()
    assert parser is not None


def test_estimate_parser_accepts_class() -> None:
    args = build_parser().parse_args(["estimate", "--class", "CTest", "--limit", "3"])
    assert args.command == "estimate"
    assert args.class_name == "CTest"
    assert args.limit == 3


def test_no_command_returns_zero() -> None:
    assert main([]) == 0


def test_version_flag() -> None:
    import pytest
    with pytest.raises(SystemExit) as exc_info:
        main(["--version"])
    assert exc_info.value.code == 0


def test_init_creates_config(tmp_path: Path) -> None:
    config_path = tmp_path / "re-agent.yaml"
    result = main(["--config", str(config_path), "init"])
    assert result == 0
    assert config_path.exists()


def test_init_fails_if_exists(tmp_path: Path) -> None:
    config_path = tmp_path / "re-agent.yaml"
    config_path.write_text("existing")
    result = main(["--config", str(config_path), "init"])
    assert result == 1


def test_status_no_session(tmp_path: Path) -> None:
    config_path = tmp_path / "re-agent.yaml"
    # Create a minimal config with session file in tmp
    config_path.write_text(f'''
output:
  session_file: "{tmp_path.as_posix()}/progress.json"
  report_dir: "{tmp_path.as_posix()}/reports"
  log_dir: "{tmp_path.as_posix()}/logs"
''')
    result = main(["--config", str(config_path), "status"])
    assert result == 0


def test_reverse_dry_run(tmp_path: Path) -> None:
    config_path = tmp_path / "re-agent.yaml"
    config_path.write_text("llm:\n  provider: claude\n")
    result = main(["--config", str(config_path), "reverse", "--address", "0x6F86A0", "--dry-run"])
    assert result == 0


def test_reverse_no_target(tmp_path: Path) -> None:
    config_path = tmp_path / "re-agent.yaml"
    config_path.write_text("llm:\n  provider: claude\n")
    result = main(["--config", str(config_path), "reverse"])
    assert result == 1
