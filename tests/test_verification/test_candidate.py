"""Tests for candidate overlays and validation gates."""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

from re_agent.config.schema import ProjectProfile, ValidationConfig
from re_agent.core.models import FunctionTarget, Verdict
from re_agent.parity.source_indexer import SourceIndexer
from re_agent.verification.candidate import (
    cleanup_candidate_overlay,
    create_candidate_overlay,
    validate_candidate,
)


def test_candidate_overlay_replaces_only_function_body(tmp_path: Path) -> None:
    source_root = tmp_path / "src"
    source_root.mkdir()
    source_file = source_root / "Train.cpp"
    source_file.write_text(
        "void CTrain::Go() { OldCall(); }\nvoid CTrain::Stop() { KeepMe(); }\n",
        encoding="utf-8",
    )
    indexer = SourceIndexer(source_root, ProjectProfile(source_root=str(source_root)))
    source = indexer.find("CTrain", "Go")
    assert source is not None

    candidate = create_candidate_overlay(
        FunctionTarget("0x100", "CTrain", "Go"),
        "void CTrain::Go() { NewCall(); }",
        source,
        source_root,
        tmp_path / "reports",
    )
    text = candidate.read_text(encoding="utf-8")
    assert "NewCall" in text
    assert "OldCall" not in text
    assert "KeepMe" in text
    assert source_file.read_text(encoding="utf-8").startswith("void CTrain::Go() { OldCall")


def test_candidate_overlay_sanitizes_qualified_class_name(tmp_path: Path) -> None:
    candidate = create_candidate_overlay(
        FunctionTarget("0x100", "app::ui::Widget", "Render"),
        "void Render() { NewCall(); }",
        None,
        tmp_path / "src",
        tmp_path / "reports",
    )
    assert candidate.exists()
    assert "::" not in candidate.name
    assert candidate.read_text(encoding="utf-8") == "void Render() { NewCall(); }\n"


def test_candidate_overlay_sanitizes_template_and_operator_names(tmp_path: Path) -> None:
    candidate = create_candidate_overlay(
        FunctionTarget("0x101", "std::vector<int, alloc>", "operator<"),
        "void operator<() {}",
        None,
        tmp_path / "src",
        tmp_path / "reports",
    )
    assert candidate.exists()
    illegal_chars = set(':<>,/\\*?"| ')
    assert not illegal_chars.intersection(candidate.name)


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX shell required")
def test_validation_gate_runs_configured_command(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.cpp"
    candidate.write_text("void f() {}", encoding="utf-8")
    verdict = validate_candidate(
        ValidationConfig(
            build_commands=["test -f '{candidate_file}'"],
            working_directory=str(tmp_path),
            trust_configured_commands=True,
        ),
        candidate,
        None,
    )
    assert verdict.verdict == Verdict.PASS


def test_required_build_without_command_fails(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.cpp"
    candidate.write_text("void f() {}", encoding="utf-8")
    verdict = validate_candidate(ValidationConfig(require_build=True), candidate, None)
    assert verdict.verdict == Verdict.FAIL


def test_nonisolated_command_must_consume_candidate(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.cpp"
    candidate.write_text("this is invalid C++", encoding="utf-8")
    verdict = validate_candidate(
        ValidationConfig(build_commands=["true"], working_directory=str(tmp_path)),
        candidate,
        None,
    )
    assert verdict.verdict == Verdict.FAIL
    assert "explicitly consume" in verdict.summary


@pytest.mark.skipif(not Path("/bin/sh").exists(), reason="POSIX shell required")
def test_untrusted_shell_gate_is_not_accepted_as_proof(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate.cpp"
    candidate.write_text("invalid C++", encoding="utf-8")
    verdict = validate_candidate(
        ValidationConfig(
            build_commands=["true # $RE_AGENT_CANDIDATE_FILE"],
            working_directory=str(tmp_path),
        ),
        candidate,
        None,
    )
    assert verdict.verdict == Verdict.UNKNOWN
    assert "trust_configured_commands" in verdict.summary


def test_failed_project_copy_creation_cleans_temporary_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    outside = tmp_path / "outside.cpp"
    outside.write_text("void CTest::Foo() {}\n", encoding="utf-8")
    indexer = SourceIndexer(tmp_path, ProjectProfile(source_root=str(tmp_path)))
    source = indexer.find("CTest", "Foo")
    assert source is not None
    overlay = tmp_path / "forced-overlay"
    monkeypatch.setattr(
        "re_agent.verification.candidate.tempfile.mkdtemp", lambda **_: str(overlay)
    )

    with pytest.raises(ValueError, match="outside validation.project_root"):
        create_candidate_overlay(
            FunctionTarget("0x100", "CTest", "Foo"),
            "void CTest::Foo() {}",
            source,
            tmp_path,
            tmp_path / "reports",
            project_root=project,
            copy_project=True,
        )

    assert not overlay.exists()


def test_copy_project_builds_against_isolated_candidate(tmp_path: Path) -> None:
    project = tmp_path / "project"
    source_root = project / "src"
    source_root.mkdir(parents=True)
    source_file = source_root / "Train.cpp"
    source_file.write_text("void CTrain::Go() { OldCall(); }\n", encoding="utf-8")
    indexer = SourceIndexer(source_root, ProjectProfile(source_root=str(source_root)))
    source = indexer.find("CTrain", "Go")
    assert source is not None

    candidate = create_candidate_overlay(
        FunctionTarget("0x100", "CTrain", "Go"),
        "void CTrain::Go() { NewCall(); }",
        source,
        source_root,
        tmp_path / "reports",
        project_root=project,
        copy_project=True,
    )
    verdict = validate_candidate(
        ValidationConfig(
            copy_project=True,
            project_root=str(project),
            build_commands=[[sys.executable, "-c",
                             "from pathlib import Path; assert 'NewCall' in Path('src/Train.cpp').read_text()"]],
            trust_configured_commands=True,
        ),
        candidate,
        str(source_file),
    )
    assert verdict.verdict == Verdict.PASS
    assert "OldCall" in source_file.read_text(encoding="utf-8")
    overlay_root = candidate.parents[1]
    cleanup_candidate_overlay(candidate)
    assert not overlay_root.exists()
