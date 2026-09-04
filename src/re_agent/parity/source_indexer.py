"""Source code indexer for C++ function body extraction and analysis."""

from __future__ import annotations

import contextlib
import re
from collections import defaultdict
from pathlib import Path

from re_agent.config.schema import ProjectProfile
from re_agent.core.models import SourceMatch
from re_agent.utils.text import (
    count_calls,
    count_control_flow,
    has_fp_token,
    strip_comments,
)

FUNC_TOKEN_RE = re.compile(r"([A-Za-z_~][A-Za-z0-9_]*)::([A-Za-z_~][A-Za-z0-9_]*)\s*\(")


class SourceIndexer:
    """Indexes C++ source files and locates function bodies by class::function name.

    The indexer uses two complementary strategies:
    1. Standard ``Class::Function(`` token scanning (always active).
    2. Project-specific ``hook_patterns`` from the profile, which can
       register additional (function_name, address) associations found
       via hook-install macros like ``RH_ScopedInstall(Func, 0xAddr)``.
    """

    def __init__(self, source_root: Path, profile: ProjectProfile | None = None) -> None:
        self.source_root = source_root
        extensions = profile.source_extensions if profile else [".cpp", ".h", ".hpp"]
        self.stub_markers = tuple(profile.stub_markers) if profile else ("NOTSA_UNREACHABLE",)
        self.stub_call_prefix = profile.stub_call_prefix if profile else "plugin::Call"
        self._hook_patterns: list[re.Pattern[str]] = []
        if profile and profile.hook_patterns:
            for pat in profile.hook_patterns:
                with contextlib.suppress(re.error):
                    self._hook_patterns.append(re.compile(pat))
        self._class_macro_re: re.Pattern[str] | None = None
        if profile and profile.class_macro:
            with contextlib.suppress(re.error):
                self._class_macro_re = re.compile(rf"{re.escape(profile.class_macro)}\s*\(\s*(\w+)\s*\)")

        self.source_files: list[Path] = sorted(p for ext in extensions for p in source_root.rglob(f"*{ext}"))
        self.file_text_cache: dict[Path, str] = {}
        self.token_index: dict[tuple[str, str], list[tuple[Path, int]]] = defaultdict(list)
        # Maps address -> (class_name, fn_name) discovered via hook patterns
        self.hook_address_index: dict[str, tuple[str, str]] = {}
        self.lookup_cache: dict[tuple[str, str], SourceMatch | None] = {}
        self.free_lookup_cache: dict[str, SourceMatch | None] = {}
        self._build_index()
        self.ast_definitions: dict[tuple[str, str], list[SourceMatch]] | None = None
        if profile and profile.compilation_database:
            from re_agent.parity.clang_index import definitions

            self.ast_definitions = defaultdict(list)
            for scope, name, path, declaration, start, end in definitions(
                Path(profile.compilation_database), source_root
            ):
                self.ast_definitions[(scope, name)].append(
                    self._make_source_match(path, self._read_text(path), declaration, start, end - 1)
                )

    def _read_text(self, path: Path) -> str:
        txt = self.file_text_cache.get(path)
        if txt is None:
            txt = path.read_text(encoding="utf-8", errors="ignore")
            self.file_text_cache[path] = txt
        return txt

    def _build_index(self) -> None:
        for path in self.source_files:
            txt = self._read_text(path)
            for m in FUNC_TOKEN_RE.finditer(txt):
                self.token_index[(m.group(1), m.group(2))].append((path, m.start()))
            # Scan hook-install macros to map addresses to function names.
            # Pattern capture groups: group(1) = func_name, group(2) = address.
            if self._hook_patterns:
                # Derive class from the file's class macro (e.g. RH_ScopedClass)
                file_class = ""
                if self._class_macro_re:
                    cm = self._class_macro_re.search(txt)
                    if cm:
                        file_class = cm.group(1)
                for hp in self._hook_patterns:
                    for hm in hp.finditer(txt):
                        if hm.lastindex and hm.lastindex >= 2:
                            fn = hm.group(1).strip()
                            addr = hm.group(2).strip().lower()
                            if fn and addr:
                                self.hook_address_index[addr] = (file_class, fn)

    @staticmethod
    def _find_matching_brace(text: str, open_brace_idx: int) -> int | None:
        depth = 0
        in_str = False
        str_quote = ""
        in_sl_comment = False
        in_ml_comment = False
        escaped = False
        i = open_brace_idx
        n = len(text)
        while i < n:
            ch = text[i]
            nxt = text[i + 1] if i + 1 < n else ""
            if in_sl_comment:
                if ch == "\n":
                    in_sl_comment = False
                i += 1
                continue
            if in_ml_comment:
                if ch == "*" and nxt == "/":
                    in_ml_comment = False
                    i += 2
                    continue
                i += 1
                continue
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == str_quote:
                    in_str = False
                i += 1
                continue
            if ch == "/" and nxt == "/":
                in_sl_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_ml_comment = True
                i += 2
                continue
            if ch in ("'", '"'):
                in_str = True
                str_quote = ch
                i += 1
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return None

    @staticmethod
    def _find_matching_paren(text: str, open_paren_idx: int) -> int | None:
        depth = 0
        in_str = False
        str_quote = ""
        in_sl_comment = False
        in_ml_comment = False
        escaped = False
        i = open_paren_idx
        n = len(text)
        while i < n:
            ch = text[i]
            nxt = text[i + 1] if i + 1 < n else ""
            if in_sl_comment:
                if ch == "\n":
                    in_sl_comment = False
                i += 1
                continue
            if in_ml_comment:
                if ch == "*" and nxt == "/":
                    in_ml_comment = False
                    i += 2
                    continue
                i += 1
                continue
            if in_str:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == str_quote:
                    in_str = False
                i += 1
                continue
            if ch == "/" and nxt == "/":
                in_sl_comment = True
                i += 2
                continue
            if ch == "/" and nxt == "*":
                in_ml_comment = True
                i += 2
                continue
            if ch in ("'", '"'):
                in_str = True
                str_quote = ch
                i += 1
                continue
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    return i
            i += 1
        return None

    @staticmethod
    def _is_inline_internal_forwarder(body_no_comments: str) -> bool:
        s = body_no_comments.strip()
        if not (s.startswith("{") and s.endswith("}")):
            return False
        inner = s[1:-1].strip()
        if not inner:
            return False
        if inner.startswith("return "):
            inner = inner[len("return ") :].strip()
        if not inner.endswith(";"):
            return False
        inner = inner[:-1].strip()
        if not inner or not inner.endswith(")"):
            return False
        open_idx = inner.find("(")
        if open_idx <= 0:
            return False
        callee = inner[:open_idx].strip()
        depth = 0
        for ch in inner[open_idx:]:
            if ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth < 0:
                    return False
        if depth != 0:
            return False
        callee_base = callee
        if callee_base.startswith("this->"):
            callee_base = callee_base[len("this->") :]
        if "::" in callee_base:
            callee_base = callee_base.split("::")[-1]
        if "<" in callee_base:
            callee_base = callee_base.split("<", 1)[0]
        return callee_base.startswith("I_") or (
            "<" in callee and len(callee_base) > 1 and callee_base.startswith("I") and callee_base[1].isupper()
        )

    def _make_source_match(self, path: Path, txt: str, idx: int, open_brace: int, close_brace: int) -> SourceMatch:
        body = txt[open_brace : close_brace + 1]
        return self.analyze_body(str(path), txt.count("\n", 0, idx) + 1, body, open_brace, close_brace + 1)

    def analyze_body(
        self,
        path: str,
        line: int,
        body: str,
        body_start: int = 0,
        body_end: int = 0,
    ) -> SourceMatch:
        """Analyze a candidate body with the same rules as indexed source."""
        body_nc = strip_comments(body)
        body_lines = body.count("\n") + 1
        total, plugin, non_plugin = count_calls(body_nc, self.stub_call_prefix)
        return SourceMatch(
            path=path,
            line=line,
            body=body,
            body_no_comments=body_nc,
            body_lines=body_lines,
            call_count=total,
            plugin_call_count=plugin,
            non_plugin_call_count=non_plugin,
            control_flow_count=count_control_flow(body_nc),
            has_stub_marker=any(marker in body_nc for marker in self.stub_markers),
            has_fp_token=has_fp_token(body_nc),
            is_inline_internal_forwarder=self._is_inline_internal_forwarder(body_nc),
            body_start=body_start,
            body_end=body_end,
        )

    def _candidate_keys(self, class_name: str, fn_name: str) -> list[tuple[str, str]]:
        keys: list[tuple[str, str]] = [(class_name, fn_name)]
        m = re.match(r"[A-Za-z_~][A-Za-z0-9_~]*", fn_name)
        if m and m.group(0) != fn_name:
            keys.append((class_name, m.group(0)))
        if fn_name == "Constructor" or fn_name.startswith("Constructor"):
            keys.insert(0, (class_name, class_name))
        elif fn_name == "Destructor" or fn_name.startswith("Destructor"):
            keys.insert(0, (class_name, f"~{class_name}"))
        seen: set[tuple[str, str]] = set()
        uniq: list[tuple[str, str]] = []
        for k in keys:
            if k in seen:
                continue
            seen.add(k)
            uniq.append(k)
        return uniq

    @staticmethod
    def _skip_ws(text: str, idx: int) -> int:
        n = len(text)
        while idx < n and text[idx].isspace():
            idx += 1
        return idx

    @staticmethod
    def _starts_with_word(text: str, idx: int, word: str) -> bool:
        end = idx + len(word)
        if not text.startswith(word, idx):
            return False
        if idx > 0 and (text[idx - 1].isalnum() or text[idx - 1] == "_"):
            return False
        return not (end < len(text) and (text[end].isalnum() or text[end] == "_"))

    def _find_function_body_open(self, txt: str, fn_idx: int, fn_name: str) -> int | None:
        paren_open = self._skip_ws(txt, fn_idx + len(fn_name))
        if paren_open >= len(txt) or txt[paren_open] != "(":
            return None
        paren_close = self._find_matching_paren(txt, paren_open)
        if paren_close is None:
            return None
        k = self._skip_ws(txt, paren_close + 1)
        while True:
            if self._starts_with_word(txt, k, "const"):
                k = self._skip_ws(txt, k + len("const"))
                continue
            if self._starts_with_word(txt, k, "override"):
                k = self._skip_ws(txt, k + len("override"))
                continue
            if self._starts_with_word(txt, k, "final"):
                k = self._skip_ws(txt, k + len("final"))
                continue
            if self._starts_with_word(txt, k, "noexcept"):
                k = self._skip_ws(txt, k + len("noexcept"))
                if k < len(txt) and txt[k] == "(":
                    nclose = self._find_matching_paren(txt, k)
                    if nclose is None:
                        return None
                    k = self._skip_ws(txt, nclose + 1)
                continue
            break
        if txt.startswith("->", k):
            k += 2
            while k < len(txt) and txt[k] not in "{;":
                k += 1
            k = self._skip_ws(txt, k)
        if k < len(txt) and txt[k] == ":":
            depth_paren = 0
            depth_brace = 0
            depth_bracket = 0
            i = k + 1
            n = len(txt)
            while i < n:
                ch = txt[i]
                if ch == "(":
                    depth_paren += 1
                elif ch == ")" and depth_paren > 0:
                    depth_paren -= 1
                elif ch == "[":
                    depth_bracket += 1
                elif ch == "]" and depth_bracket > 0:
                    depth_bracket -= 1
                elif ch == "{":
                    if depth_paren == 0 and depth_brace == 0 and depth_bracket == 0:
                        j = i - 1
                        while j >= 0 and txt[j].isspace():
                            j -= 1
                        prev = txt[j] if j >= 0 else ""
                        if prev.isalnum() or prev in "_>":
                            depth_brace += 1
                        else:
                            return i
                    else:
                        depth_brace += 1
                elif ch == "}" and depth_brace > 0:
                    depth_brace -= 1
                elif ch == ";" and depth_paren == 0 and depth_brace == 0 and depth_bracket == 0:
                    return None
                i += 1
            return None
        k = self._skip_ws(txt, k)
        if k >= len(txt) or txt[k] != "{":
            return None
        if txt.find(";", fn_idx, k) != -1:
            return None
        return k

    def _is_free_function_definition(self, txt: str, fn_idx: int, fn_name: str) -> int | None:
        j = fn_idx - 1
        while j >= 0 and txt[j].isspace():
            j -= 1
        if j < 0:
            return None
        if txt[j] not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789_&*>:":
            return None
        return self._find_function_body_open(txt, fn_idx, fn_name)

    def find_by_address(self, address: str) -> SourceMatch | None:
        """Look up a source function body by its hook address.

        Uses the ``hook_address_index`` built from hook-install macros to
        resolve *address* → *(class_name, fn_name)* and then delegates to
        :meth:`find`.  Returns ``None`` if the address is not in the index.
        """
        addr_key = address.strip().lower()
        entry = self.hook_address_index.get(addr_key)
        if entry is None:
            return None
        cls, fn = entry
        if not fn:
            return None
        return self.find(cls, fn)

    def _find_free_function(self, fn_name: str) -> SourceMatch | None:
        if not fn_name:
            return None
        if fn_name in self.free_lookup_cache:
            return self.free_lookup_cache[fn_name]
        pattern = re.compile(rf"(?<!::)\b{re.escape(fn_name)}\s*\(")
        for path in self.source_files:
            txt = self._read_text(path)
            for m in pattern.finditer(txt):
                idx = m.start()
                open_brace = self._is_free_function_definition(txt, idx, fn_name)
                if open_brace is None:
                    continue
                close_brace = self._find_matching_brace(txt, open_brace)
                if close_brace is None:
                    continue
                sm = self._make_source_match(path, txt, idx, open_brace, close_brace)
                self.free_lookup_cache[fn_name] = sm
                return sm
        self.free_lookup_cache[fn_name] = None
        return None

    def find(self, class_name: str, fn_name: str) -> SourceMatch | None:
        if self.ast_definitions is not None:
            matches = self.ast_definitions.get((class_name, fn_name), [])
            return matches[0] if len(matches) == 1 else None
        if not fn_name and not class_name:
            return None
        key = (class_name, fn_name)
        if key in self.lookup_cache:
            return self.lookup_cache[key]
        if not fn_name:
            self.lookup_cache[key] = None
            return None
        for candidate_key in self._candidate_keys(class_name, fn_name):
            candidates = self.token_index.get(candidate_key, [])
            for path, idx in candidates:
                txt = self._read_text(path)
                fn_start = idx + len(candidate_key[0]) + 2
                open_brace = self._find_function_body_open(txt, fn_start, candidate_key[1])
                if open_brace is None:
                    continue
                close_brace = self._find_matching_brace(txt, open_brace)
                if close_brace is None:
                    continue
                sm = self._make_source_match(path, txt, idx, open_brace, close_brace)
                self.lookup_cache[key] = sm
                return sm
        free = self._find_free_function(fn_name) if not class_name else None
        if free is not None:
            self.lookup_cache[key] = free
            return free
        self.lookup_cache[key] = None
        return None

    def find_all(self, class_name: str, fn_name: str) -> list[SourceMatch]:
        """Return every matching definition so overloads can be handled conservatively."""
        if self.ast_definitions is not None:
            return list(self.ast_definitions.get((class_name, fn_name), []))
        if not fn_name:
            return []
        if not class_name:
            free_matches = []
            pattern = re.compile(rf"(?<!::)\b{re.escape(fn_name)}\s*\(")
            for path in self.source_files:
                txt = self._read_text(path)
                for match in pattern.finditer(txt):
                    opening = self._is_free_function_definition(txt, match.start(), fn_name)
                    if opening is None:
                        continue
                    closing = self._find_matching_brace(txt, opening)
                    if closing is not None:
                        free_matches.append(self._make_source_match(path, txt, match.start(), opening, closing))
            return free_matches
        matches: list[SourceMatch] = []
        seen: set[tuple[Path, int]] = set()
        for candidate_key in self._candidate_keys(class_name, fn_name):
            for path, idx in self.token_index.get(candidate_key, []):
                if (path, idx) in seen:
                    continue
                txt = self._read_text(path)
                fn_start = idx + len(candidate_key[0]) + 2
                open_brace = self._find_function_body_open(txt, fn_start, candidate_key[1])
                if open_brace is None:
                    continue
                close_brace = self._find_matching_brace(txt, open_brace)
                if close_brace is None:
                    continue
                seen.add((path, idx))
                matches.append(self._make_source_match(path, txt, idx, open_brace, close_brace))
        return matches
