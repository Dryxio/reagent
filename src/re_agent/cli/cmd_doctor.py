"""Read-only configuration and evidence preflight."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path

from re_agent.backend.registry import create_backend
from re_agent.config.loader import load_config


def cmd_doctor(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    checks: list[dict[str, object]] = []

    def add(name: str, passed: bool, detail: str = "") -> None:
        checks.append({"check": name, "passed": passed, "detail": detail})

    add("source_root", Path(config.project_profile.source_root).is_dir(), config.project_profile.source_root)
    for role, model in (
        ("reverser", config.agents.reverser or config.llm),
        ("checker", config.agents.checker or config.llm),
    ):
        if model.provider in {"claude-cli", "codex"}:
            executable = model.cli_path or ("claude" if model.provider == "claude-cli" else "codex")
            add(role + " executable", shutil.which(executable) is not None, executable)
    validation = config.validation
    commands = validation.build_commands + validation.test_commands + validation.runtime_commands
    if validation.enabled and any(isinstance(command, str) for command in commands):
        add("validation shell", shutil.which("/bin/sh") is not None,
            "Shell strings require /bin/sh; use argument arrays for native Windows validation")
    has_gates = bool(
        validation.build_commands
        or validation.test_commands
        or validation.runtime_commands
        or validation.differential_cases_file
    )
    add(
        "acceptance policy",
        not validation.enabled
        or not validation.require_verified
        or (has_gates and validation.trust_configured_commands),
        "Verified acceptance requires configured, explicitly trusted validation gates",
    )
    for required, commands, name in [
        (validation.require_build, validation.build_commands, "build"),
        (validation.require_tests, validation.test_commands, "tests"),
        (validation.require_runtime, validation.runtime_commands, "runtime"),
    ]:
        add(name + " gate", not validation.enabled or not required or bool(commands))
    try:
        backend = create_backend(config.backend)
        add("decompile capability", backend.capabilities.has_decompile)
        if args.address:
            result = backend.decompile(args.address)
            add("target evidence", bool(result.decompiled.strip()), result.name)
    except (OSError, ValueError, RuntimeError) as exc:
        add("backend", False, str(exc))
    print(json.dumps({"checks": checks, "ready": all(c["passed"] for c in checks)}, indent=2))
    return 0 if all(c["passed"] for c in checks) else 1
