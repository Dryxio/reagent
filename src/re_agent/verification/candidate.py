"""Candidate overlays and configurable build/test validation gates."""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

from re_agent.config.schema import ValidationConfig
from re_agent.core.models import FunctionTarget, SourceMatch, ValidationVerdict, Verdict
from re_agent.utils.process import run_process

_NON_CODE = re.compile(
    r'//[^\n]*|/\*[\s\S]*?\*/|R"(?P<delimiter>[^ ()\\\t\r\n]{0,16})\([\s\S]*?\)(?P=delimiter)"'
    r'|"(?:\\.|[^"\\])*"|\'(?:\\.|[^\'\\])*\''
)


def unresolved_placeholders(code: str) -> list[str]:
    """Find executable unrecovered-jump placeholders, ignoring comments/literals."""
    tokens = _NON_CODE.sub(" ", code)
    return sorted(set(re.findall(r"\bUNRECOVERED_JUMPTABLE(?:_\w+)?\b", tokens)))


def extract_candidate_body(code: str) -> str:
    """Extract the outer C++ body from generated code."""
    from re_agent.parity.source_indexer import SourceIndexer

    if code.lstrip().startswith(("namespace ", "class ", "struct ")):
        raise ValueError("Candidate must contain exactly one function, without namespace/class wrappers")
    open_brace = code.find("{")
    if open_brace < 0:
        raise ValueError("Candidate has no function body")
    close_brace = SourceIndexer._find_matching_brace(code, open_brace)
    if close_brace is None or code[close_brace + 1 :].strip().strip(";"):
        raise ValueError("Candidate must contain exactly one complete function body")
    return code[open_brace : close_brace + 1].strip()


def create_candidate_overlay(
    target: FunctionTarget,
    code: str,
    source: SourceMatch | None,
    source_root: Path,
    report_dir: Path,
    project_root: Path | None = None,
    copy_project: bool = False,
) -> Path:
    """Write a source overlay with the original function body replaced."""
    safe_address = _sanitize_path_component(target.address)
    overlay_root: Path | None = None
    try:
        if copy_project:
            if project_root is None:
                raise ValueError("project_root is required when copy_project is enabled")
            overlay_root = Path(tempfile.mkdtemp(prefix=f"re-agent-{safe_address}-"))
            shutil.copytree(
                project_root,
                overlay_root,
                dirs_exist_ok=True,
                symlinks=True,
                ignore=shutil.ignore_patterns(".git", ".venv", "build", "reports", "__pycache__", "*.pyc"),
            )
            _remap_links(overlay_root, project_root)
        else:
            overlay_root = report_dir / "candidates" / safe_address
        overlay_root.mkdir(parents=True, exist_ok=True)
        (overlay_root / ".re-agent-overlay").write_text("schema_version=1\n", encoding="utf-8")
        if source is None:
            safe_class_name = _sanitize_path_component(target.class_name)
            safe_function_name = _sanitize_path_component(target.function_name)
            candidate_file = overlay_root / f"{safe_class_name}_{safe_function_name}.cpp"
            candidate_file.parent.mkdir(parents=True, exist_ok=True)
            candidate_file.write_text(code.rstrip() + "\n", encoding="utf-8")
            return candidate_file

        original_path = Path(source.path)
        relative_root = project_root if copy_project and project_root is not None else source_root
        try:
            relative = original_path.resolve().relative_to(relative_root.resolve())
        except ValueError:
            if copy_project:
                raise ValueError(
                    f"Source file {original_path} is outside validation.project_root {relative_root}"
                ) from None
            relative = Path(original_path.name)
        candidate_file = overlay_root / relative
        candidate_file.parent.mkdir(parents=True, exist_ok=True)

        original = original_path.read_text(encoding="utf-8", errors="ignore")
        if source.body_end <= source.body_start:
            raise ValueError(f"Source body offsets unavailable for {source.path}")
        body = extract_candidate_body(code)
        overlaid = original[: source.body_start] + body + original[source.body_end :]
        candidate_file.write_text(overlaid, encoding="utf-8")
        return candidate_file
    except Exception:
        if copy_project and overlay_root is not None:
            shutil.rmtree(overlay_root, ignore_errors=True)
        raise


def validate_candidate(
    config: ValidationConfig,
    candidate_file: Path,
    source_file: str | None,
) -> ValidationVerdict:
    """Run configured build and test commands against the candidate overlay."""
    commands = [("build", command) for command in config.build_commands]
    commands.extend(("test", command) for command in config.test_commands)
    commands.extend(("runtime", command) for command in config.runtime_commands)
    if not config.enabled:
        return ValidationVerdict(
            verdict=Verdict.UNKNOWN,
            summary="Candidate validation disabled",
            overlay_file=str(candidate_file),
        )
    if config.require_build and not config.build_commands:
        return _failed("Build validation is required but no build_commands are configured", candidate_file)
    if config.require_tests and not config.test_commands:
        return _failed("Test validation is required but no test_commands are configured", candidate_file)
    if config.require_runtime and not config.runtime_commands:
        return _failed("Runtime validation is required but no runtime_commands are configured", candidate_file)
    if (
        source_file is None
        and config.copy_project
        and commands
        and not any("{candidate_file}" in part for _, command in commands
                    for part in ([command] if isinstance(command, str) else command))
    ):
        return _failed(
            "Candidate has no source location; isolated project commands must explicitly use {candidate_file}",
            candidate_file,
        )
    if not commands and not config.differential_cases_file:
        return ValidationVerdict(
            verdict=Verdict.UNKNOWN,
            summary="Candidate overlay created; no build or test commands configured",
            overlay_file=str(candidate_file),
        )
    if not config.copy_project:
        unsafe = [command for _, command in commands if not _consumes_candidate(command)]
        if unsafe:
            return _failed(
                "Non-isolated validation commands must explicitly consume "
                "{candidate_file}, {overlay_root}, RE_AGENT_CANDIDATE_FILE, or "
                "RE_AGENT_OVERLAY_ROOT",
                candidate_file,
                [f"does not consume candidate: {command}" for command in unsafe],
            )

    env = os.environ.copy()
    env.update(
        {
            "RE_AGENT_CANDIDATE_FILE": str(candidate_file.resolve()),
            "RE_AGENT_OVERLAY_ROOT": str(_overlay_root(candidate_file).resolve()),
            "RE_AGENT_SOURCE_FILE": source_file or "",
        }
    )
    findings: list[str] = []
    checks: list[dict[str, str]] = []
    for kind, command in commands:
        expanded = (
            ["/bin/sh", "-lc", _expand_shell(command)] if isinstance(command, str)
            else [_expand_argument(arg, env) for arg in command]
        )
        try:
            proc = run_process(
                expanded,
                cwd=_working_directory(config, candidate_file),
                env=env,
                timeout_s=config.command_timeout_s,
            )
        except OSError as exc:
            checks.append({"kind": kind, "verdict": "FAIL", "detail": str(exc)})
            return _failed(f"{kind} command could not start: {exc}", candidate_file, findings, checks)
        except subprocess.TimeoutExpired:
            checks.append({"kind": kind, "verdict": "FAIL", "detail": "timed out"})
            return _failed(f"{kind} command timed out: {command}", candidate_file, findings, checks)
        # First errors often explain cascades of missing types/declarations.
        # Keep them alongside the final summary, prioritizing compiler stderr.
        lines = (proc.stderr + "\n" + proc.stdout).strip().splitlines()
        if len(lines) > 20:
            lines = [*lines[:10], "[intermediate output omitted]", *lines[-10:]]
        excerpt = "\n".join(line[:500] for line in lines)
        findings.append(f"{kind}: {command} -> exit {proc.returncode}\n{excerpt}".rstrip())
        checks.append({"kind": kind, "verdict": "PASS" if proc.returncode == 0 else "FAIL",
                       "detail": f"exit {proc.returncode}"})
        if proc.returncode != 0:
            return _failed(f"Candidate {kind} gate failed", candidate_file, findings, checks)

    if config.differential_cases_file:
        from re_agent.verification.differential import compare_commands

        try:
            cases = json.loads(Path(config.differential_cases_file).read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            checks.append({"kind": "differential", "verdict": "FAIL", "detail": str(exc)})
            return _failed("Could not read differential cases", candidate_file, findings, checks)
        if not isinstance(cases, list):
            checks.append({"kind": "differential", "verdict": "FAIL", "detail": "Cases must be a JSON array"})
            return _failed("Differential cases must be a JSON array", candidate_file, findings, checks)

        def expand_args(args: list[str]) -> list[str]:
            values = {
                "{candidate_file}": str(candidate_file.resolve()),
                "{overlay_root}": str(_overlay_root(candidate_file).resolve()),
            }
            result = []
            for arg in args:
                for key, value in values.items():
                    arg = arg.replace(key, value)
                result.append(arg)
            return result

        comparison = compare_commands(
            expand_args(config.differential_reference),
            expand_args(config.differential_candidate),
            cases,
            Path(_working_directory(config, candidate_file)),
            config.command_timeout_s,
        )
        checks.append({"kind": "differential", "verdict": "PASS" if comparison.passed else "FAIL",
                       "detail": f"{comparison.cases_run} cases"})
        findings.extend(comparison.findings)
        if not comparison.passed:
            return _failed("Candidate differential gate failed", candidate_file, findings, checks)

    if not config.trust_configured_commands:
        return ValidationVerdict(
            verdict=Verdict.UNKNOWN,
            summary=(
                "Configured commands passed but are not accepted as proof until "
                "validation.trust_configured_commands is explicitly enabled"
            ),
            findings=findings,
            checks=checks,
            overlay_file=str(candidate_file),
        )

    return ValidationVerdict(
        verdict=Verdict.PASS,
        summary="All configured candidate validation gates passed",
        findings=findings,
        checks=checks,
        overlay_file=str(candidate_file),
    )


def cleanup_candidate_overlay(candidate_file: Path) -> None:
    """Remove a temporary full-project overlay created for isolated validation."""
    root = _overlay_root(candidate_file)
    if root.name.startswith("re-agent-") and (root / ".re-agent-overlay").exists():
        shutil.rmtree(root, ignore_errors=True)


def _sanitize_path_component(value: str) -> str:
    """Replace characters that are illegal in host filesystem names."""
    return re.sub(r"[^A-Za-z0-9_.-]", "_", value)


def _expand_argument(argument: str, env: dict[str, str]) -> str:
    # Single pass: replacement values are data, never additional placeholders.
    return re.sub(
        r"\{(candidate_file|overlay_root|source_file)\}",
        lambda match: env["RE_AGENT_" + match[1].upper()], argument,
    )


def _consumes_candidate(command: str | list[str]) -> bool:
    if isinstance(command, list):
        return any(marker in arg for arg in command for marker in ("{candidate_file}", "{overlay_root}"))
    markers = (
        "{candidate_file}",
        "{overlay_root}",
        "$RE_AGENT_CANDIDATE_FILE",
        "${RE_AGENT_CANDIDATE_FILE}",
        "$RE_AGENT_OVERLAY_ROOT",
        "${RE_AGENT_OVERLAY_ROOT}",
    )
    return any(marker in command for marker in markers)


def _overlay_root(candidate_file: Path) -> Path:
    for parent in (candidate_file.parent, *candidate_file.parents):
        if (parent / ".re-agent-overlay").exists():
            return parent
    parts = candidate_file.parts
    if "candidates" in parts:
        idx = parts.index("candidates")
        if idx + 1 < len(parts):
            return Path(*parts[: idx + 2])
    return candidate_file.parent


def _working_directory(config: ValidationConfig, candidate_file: Path) -> str:
    overlay_root = str(_overlay_root(candidate_file).resolve())
    value = config.working_directory.replace("{overlay_root}", overlay_root)
    if config.copy_project and config.working_directory == ".":
        return overlay_root
    if config.copy_project:
        resolved = (Path(overlay_root) / value).resolve()
        if not resolved.is_relative_to(Path(overlay_root).resolve()):
            raise ValueError("Isolated validation working_directory must remain inside the overlay")
        return str(resolved)
    return value


def _failed(
    summary: str,
    candidate_file: Path,
    findings: list[str] | None = None,
    checks: list[dict[str, str]] | None = None,
) -> ValidationVerdict:
    return ValidationVerdict(
        verdict=Verdict.FAIL,
        summary=summary,
        findings=findings or [],
        checks=checks or [],
        overlay_file=str(candidate_file),
    )


def _remap_links(overlay: Path, project: Path) -> None:
    """Keep internal links inside the copy; reject external and broken links."""
    root = project.resolve()
    for directory, dirs, files in os.walk(overlay, followlinks=False):
        for name in [*dirs, *files]:
            link = Path(directory) / name
            if not link.is_symlink():
                continue
            original = root / link.relative_to(overlay)
            try:
                target = original.resolve(strict=True).relative_to(root)
            except (ValueError, OSError, RuntimeError) as exc:
                raise ValueError(f"Cannot isolate source symlink: {original}") from exc
            link.unlink()
            link.symlink_to(os.path.relpath(overlay / target, link.parent), target_is_directory=original.is_dir())


def _expand_shell(command: str) -> str:
    """Expand placeholders via environment values, respecting existing quotes."""
    markers = {
        "{candidate_file}": "RE_AGENT_CANDIDATE_FILE",
        "{overlay_root}": "RE_AGENT_OVERLAY_ROOT",
        "{source_file}": "RE_AGENT_SOURCE_FILE",
    }
    quote = ""
    output = ""
    index = 0
    while index < len(command):
        marker = next((m for m in markers if command.startswith(m, index)), None)
        if marker:
            variable = "${" + markers[marker] + "}"
            if quote == "'":
                output += "'\"" + variable + "\"'"
            elif quote == '"':
                output += variable
            else:
                output += '"' + variable + '"'
            index += len(marker)
            continue
        char = command[index]
        if char == "\\" and quote != "'" and index + 1 < len(command):
            output += command[index : index + 2]
            index += 2
            continue
        if char in {"'", '"'}:
            if not quote:
                quote = char
            elif quote == char:
                quote = ""
        output += char
        index += 1
    return output
