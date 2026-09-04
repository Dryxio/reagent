"""Source-context retrieval for the reverser prompt."""
from __future__ import annotations

import re
from pathlib import Path

from re_agent.config.schema import ProjectProfile
from re_agent.core.models import FunctionTarget
from re_agent.core.session import Session
from re_agent.parity.source_indexer import SourceIndexer


class SourceContextBuilder:
    """Build retrieval context from nearby source, headers, and recent outputs."""

    def __init__(
        self,
        source_root: Path,
        profile: ProjectProfile,
        indexer: SourceIndexer | None = None,
        session: Session | None = None,
        report_dir: Path | None = None,
        max_chars: int = 12_000,
    ) -> None:
        self.source_root = source_root
        self.profile = profile
        self.indexer = indexer or SourceIndexer(source_root, profile)
        self.session = session
        self.report_dir = report_dir
        self.max_chars = max_chars

    def build(self, target: FunctionTarget) -> str:
        sections: list[str] = []

        header = self._find_class_header(target.class_name)
        if header:
            sections.append("Class header:\n" + header)

        siblings = self._find_sibling_methods(target)
        if siblings:
            sections.append("Sibling methods:\n" + "\n\n".join(siblings))

        recent = self._find_recent_generated_code(target)
        if recent:
            sections.append("Recent accepted reversals (see validation status):\n" + "\n\n".join(recent))

        if not sections:
            return "No relevant existing source context found."

        combined = "\n\n".join(sections)
        if len(combined) <= self.max_chars:
            return combined
        return combined[: self.max_chars - 17].rstrip() + "\n\n[truncated]"

    def _find_class_header(self, class_name: str) -> str:
        if not class_name:
            return ""
        class_re = re.compile(rf"\b(class|struct)\s+{re.escape(class_name)}\b")
        for path in self.source_root.rglob("*"):
            if path.suffix not in {".h", ".hpp", ".hh"}:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            match = class_re.search(text)
            if match is None:
                continue
            start_line = text.count("\n", 0, match.start()) + 1
            lines = text.splitlines()
            snippet = "\n".join(lines[start_line - 1:start_line + 24])
            return f"{path}:{start_line}\n```cpp\n{snippet}\n```"
        return ""

    def _find_sibling_methods(self, target: FunctionTarget) -> list[str]:
        methods: list[str] = []
        class_name = target.class_name
        if not class_name:
            return methods

        target_match = self.indexer.find(class_name, target.function_name)
        preferred_path = Path(target_match.path) if target_match is not None else None

        sibling_names = sorted({
            fn_name
            for cls_name, fn_name in self.indexer.token_index
            if cls_name == class_name and fn_name != target.function_name
        })

        def rank(fn_name: str) -> tuple[int, str]:
            match = self.indexer.find(class_name, fn_name)
            same_file = int(match is not None and preferred_path is not None and Path(match.path) == preferred_path)
            return (-same_file, fn_name)

        for fn_name in sorted(sibling_names, key=rank)[:3]:
            match = self.indexer.find(class_name, fn_name)
            if match is None:
                continue
            body = self._trim_block(match.body, max_lines=22)
            methods.append(f"{match.path}:{match.line}\n```cpp\n{body}\n```")
        return methods

    def _find_recent_generated_code(self, target: FunctionTarget) -> list[str]:
        if self.session is None or self.report_dir is None:
            return []

        out: list[str] = []
        code_dir = self.report_dir / "code"
        if not code_dir.exists():
            return out

        candidates = []
        for entry in reversed(self.session.get_all_functions()):
            if not entry.get("success"):
                continue
            if entry.get("class_name") != target.class_name:
                continue
            if entry.get("function_name") == target.function_name:
                continue
            candidates.append(entry)

        for entry in candidates[:2]:
            path = code_dir / self._code_filename(
                entry.get("address", ""),
                entry.get("class_name", ""),
                entry.get("function_name", ""),
            )
            if not path.exists():
                continue
            try:
                body = path.read_text(encoding="utf-8")
            except OSError:
                continue
            snippet = self._trim_block(body, max_lines=30)
            out.append(f"{path}\n```cpp\n{snippet}\n```")
        return out

    @staticmethod
    def _trim_block(text: str, max_lines: int) -> str:
        lines = text.strip().splitlines()
        if len(lines) <= max_lines:
            return "\n".join(lines)
        return "\n".join(lines[:max_lines]) + "\n// ..."

    @staticmethod
    def _code_filename(address: str, class_name: str, function_name: str) -> str:
        safe_name = f"{address}_{class_name}_{function_name}.cpp"
        from re_agent.verification.candidate import _sanitize_path_component
        return _sanitize_path_component(safe_name)
