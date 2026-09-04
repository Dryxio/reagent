"""Optional C++ definition indexing using a project's compilation database."""

from __future__ import annotations

import json
import shlex
import subprocess
from pathlib import Path
from typing import Any


def definitions(database: Path, source_root: Path) -> list[tuple[str, str, Path, int, int, int]]:
    """Return (scope, name, file, declaration, body-start, body-end).

    Uses each translation unit's actual compile flags. Unsupported commands or
    compiler errors are explicit failures; they never silently fall back to regex.
    """
    commands = json.loads(database.read_text(encoding="utf-8"))
    if not isinstance(commands, list):
        raise ValueError("Compilation database must be a JSON array")
    result: list[tuple[str, str, Path, int, int, int]] = []
    seen: set[tuple[str, int]] = set()
    root = source_root.resolve()
    for item in commands:
        cwd = Path(item["directory"])
        file = (cwd / item["file"]).resolve()
        if not file.is_relative_to(root):
            continue
        args = item.get("arguments") or shlex.split(item["command"])
        # Preserve compiler flags, remove output/compile-only flags.
        cleaned: list[str] = []
        skip = False
        for arg in args[1:]:
            if skip:
                skip = False
                continue
            if arg in {"-o", "-MF", "-MT", "-MQ"}:
                skip = True
                continue
            if arg in {"-c", "-MD", "-MMD", "-MP"}:
                continue
            cleaned.append(arg)
        proc = subprocess.run(
            ["clang++", *cleaned, "-fsyntax-only", "-Xclang", "-ast-dump=json"],
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if proc.returncode:
            raise ValueError(f"Clang could not index {file}: {proc.stderr[-4000:]}")
        tree = json.loads(proc.stdout)
        contexts: dict[str, str] = {}

        def walk(
            node: dict[str, Any], scope: str = "", inherited: Path = file, contexts: dict[str, str] = contexts
        ) -> None:
            loc = node.get("loc", {})
            current_file = Path(loc.get("file", inherited)).resolve()
            kind, name = node.get("kind"), str(node.get("name", ""))
            next_scope = scope
            if kind in {"NamespaceDecl", "CXXRecordDecl", "RecordDecl"} and name and not node.get("isImplicit"):
                next_scope = "::".join(filter(None, (scope, name)))
                contexts[str(node.get("id"))] = next_scope
            if kind in {
                "FunctionDecl",
                "CXXMethodDecl",
                "CXXConstructorDecl",
                "CXXDestructorDecl",
                "CXXConversionDecl",
            }:
                owner = contexts.get(str(node.get("parentDeclContextId")), scope)
                for child in node.get("inner", []):
                    if child.get("kind") != "CompoundStmt" or not current_file.is_relative_to(root):
                        continue
                    bounds = child.get("range", {})
                    begin, end = bounds.get("begin", {}), bounds.get("end", {})
                    # Macro-expanded definitions require a separate rewriting policy.
                    if "offset" not in begin or "offset" not in end:
                        raise ValueError(f"Macro-expanded body cannot be safely replaced: {owner}::{name}")
                    start, stop = int(begin["offset"]), int(end["offset"]) + int(end.get("tokLen", 1))
                    key = (str(current_file), start)
                    if key not in seen:
                        seen.add(key)
                        declaration = int(loc.get("offset", start))
                        # Clang uses UTF-8 byte offsets; the indexer stores character offsets.
                        raw = current_file.read_bytes()
                        offsets = [len(raw[:pos].decode("utf-8")) for pos in (declaration, start, stop)]
                        result.append((owner, name, current_file, offsets[0], offsets[1], offsets[2]))
            for child in node.get("inner", []):
                if isinstance(child, dict):
                    walk(child, next_scope, current_file)

        walk(tree)
    return result
