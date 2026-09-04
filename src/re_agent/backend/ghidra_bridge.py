"""Ghidra CLI bridge backend implementation."""

from __future__ import annotations

import re

from re_agent.backend.protocol import BackendCapabilities
from re_agent.core.models import (
    AnalysisArtifact,
    AsmResult,
    DecompileResult,
    EnumDef,
    EnumValue,
    FunctionEntry,
    StructDef,
    StructField,
    XRef,
)
from re_agent.utils.process import run_cmd, run_cmd_split


class GhidraBridgeBackend:
    """Backend that shells out to a Ghidra CLI tool.

    The CLI is expected to expose sub-commands such as ``decompile``,
    ``xrefs-to``, ``xrefs-from``, ``source-struct``, ``source-enum``,
    ``asm``, ``search``, ``unimplemented``, and ``remaining``.

    Args:
        cli_path: Path (or command name) for the Ghidra CLI tool.
        timeout_s: Maximum seconds per CLI invocation.
    """

    def __init__(self, cli_path: str = "ghidra-bridge", timeout_s: int = 45) -> None:
        self._cli_path = cli_path
        self._timeout_s = timeout_s
        self._caps: BackendCapabilities | None = None
        self._response_cache: dict[tuple[str, ...], str] = {}

    # -- helpers --------------------------------------------------------------

    def _run(self, *args: str) -> str:
        """Execute the Ghidra CLI and return stdout.

        Raises:
            RuntimeError: If the command exits with non-zero status.
        """
        key = tuple(args)
        if key in self._response_cache:
            return self._response_cache[key]
        ok, output = run_cmd([self._cli_path, *args], self._timeout_s)
        if not ok:
            raise RuntimeError(f"Ghidra CLI failed: {self._cli_path} {' '.join(args)}\n{output}")
        self._response_cache[key] = output
        return output

    def _try_run(self, *args: str) -> str | None:
        """Execute the Ghidra CLI and return stdout, or ``None`` on failure."""
        key = tuple(args)
        if key in self._response_cache:
            return self._response_cache[key]
        ok, output = run_cmd([self._cli_path, *args], self._timeout_s)
        if not ok:
            return None
        self._response_cache[key] = output
        return output

    # -- capabilities ---------------------------------------------------------

    @property
    def capabilities(self) -> BackendCapabilities:
        """Return detected backend capabilities (lazy-initialised)."""
        if self._caps is None:
            self._caps = self._probe_capabilities()
        return self._caps

    # Patterns in stderr that indicate the sub-command itself is unrecognised
    # (as opposed to a valid sub-command that failed on bad input).
    _UNKNOWN_CMD_PATTERNS = (
        "unknown command",
        "unrecognized command",
        "invalid choice",
        "no such sub-command",
        "not a command",
    )

    def _subcmd_exists(self, subcmd: str) -> bool:
        """Return True if *subcmd* is recognised by the CLI.

        A sub-command is considered available when:
        - It exits 0 (clearly works), **or**
        - It exits non-zero but stderr does NOT contain an
          "unknown command"-style error message.  This covers CLIs that
          return non-zero for ``--help`` or for bad arguments while still
          recognising the sub-command.
        """
        rc, _stdout, stderr = run_cmd_split([self._cli_path, subcmd, "--help"], timeout_s=min(self._timeout_s, 10))
        if rc < 0:
            return False
        if rc == 0:
            return True
        stderr_lower = stderr.lower()
        if any(pat in stderr_lower for pat in self._UNKNOWN_CMD_PATTERNS):
            return False
        # Non-zero but no "unknown command" — likely just bad args for
        # a valid sub-command.  Double-check with a dummy invocation.
        rc2, _stdout2, stderr2 = run_cmd_split(
            [self._cli_path, subcmd, "__probe__"], timeout_s=min(self._timeout_s, 10)
        )
        if rc2 < 0:
            return False
        if rc2 == 0:
            return True
        stderr2_lower = stderr2.lower()
        return not any(pat in stderr2_lower for pat in self._UNKNOWN_CMD_PATTERNS)

    def _probe_capabilities(self) -> BackendCapabilities:
        """Probe the CLI to detect which sub-commands are actually available.

        Uses stderr content inspection rather than exit-code alone, so
        CLIs that return non-zero for ``--help`` or probe invocations are
        handled correctly.
        """
        caps = BackendCapabilities(has_decompile=self._subcmd_exists("decompile"))

        probes: list[tuple[str, str]] = [
            ("has_asm", "asm"),
            ("has_structs", "source-struct"),
            ("has_xrefs", "xrefs-from"),
            ("has_search", "search"),
            ("has_enums", "source-enum"),
            ("has_context", "context"),
            ("has_vtables", "vtable"),
            ("has_globals", "global"),
            ("has_strings", "strings"),
            ("has_pcode", "pcode"),
            ("has_cfg", "cfg"),
        ]
        for attr, subcmd in probes:
            setattr(caps, attr, self._subcmd_exists(subcmd))

        return caps

    # -- decompile ------------------------------------------------------------

    def decompile(self, target: str) -> DecompileResult:
        """Decompile a function by address or symbol name."""
        raw = self._run("decompile", target)

        # Attempt to parse callers/callees from a line like:
        #   "Callers: 5 | Callees: 3"
        callers: int | None = None
        callees: int | None = None
        m = re.search(r"Callers:\s*(\d+)\s*\|\s*Callees:\s*(\d+)", raw)
        if m:
            callers = int(m.group(1))
            callees = int(m.group(2))

        # Try to extract the function name from the first meaningful line.
        name = target
        for line in raw.splitlines():
            stripped = line.strip()
            if stripped and not stripped.startswith("//") and not stripped.startswith("Callers"):
                # Heuristic: take the first line that looks like a signature.
                name = stripped.split("(")[0].split()[-1] if "(" in stripped else target
                break

        known = re.search(r"^// Known as:\s*(.+)$", raw, re.M)
        if known:
            name = known.group(1).strip()
        signature_match = re.search(r"^// Signature:\s*(.+)$", raw, re.M)
        return DecompileResult(
            address=target,
            name=name,
            signature=signature_match.group(1).strip() if signature_match else "",
            decompiled=raw,
            raw_output=raw,
            callers=callers,
            callees=callees,
        )

    # -- xrefs ----------------------------------------------------------------

    def xrefs_to(self, target: str) -> list[XRef]:
        """Parse cross-references TO a function."""
        raw = self._run("xrefs-to", target)
        return self._parse_xrefs(raw, ref_type="CALL")

    def xrefs_from(self, target: str) -> list[XRef]:
        """Parse cross-references FROM a function."""
        raw = self._run("xrefs-from", target)
        return self._parse_xrefs(raw, ref_type="CALL")

    @staticmethod
    def _parse_xrefs(raw: str, ref_type: str = "CALL") -> list[XRef]:
        """Parse xref output lines into XRef objects.

        Expected format per line: ``0xADDRESS  FunctionName`` or similar.
        """
        results: list[XRef] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or line.startswith("=") or line.startswith("//"):
                continue
            # Try to split into address + name
            parts = line.split(None, 1)
            if parts:
                addr = parts[0]
                if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", addr):
                    continue
                name = parts[1] if len(parts) > 1 else ""
                results.append(XRef(address=addr, name=name, ref_type=ref_type))
        return results

    # -- struct ---------------------------------------------------------------

    def get_struct(self, name: str) -> StructDef | None:
        """Retrieve a struct definition by name."""
        raw = self._try_run("source-struct", name)
        if raw is None:
            return None

        # Parse size from a line like "Size: 0x1234 (4660)"
        size = 0
        m = re.search(r"Size:\s*(?:0x)?([0-9a-fA-F]+)", raw)
        if m:
            size = int(m.group(1), 16)

        # Parse fields from lines like:
        #   "+0x0040  int32_t  m_nPhysicalFlags"
        fields: list[StructField] = []
        for line in raw.splitlines():
            legacy = re.match(r"\s*//\s*(0x[0-9a-fA-F]+)\s+(\w+)\s*$", line)
            if legacy:
                fields.append(
                    StructField(name=legacy.group(2), offset=int(legacy.group(1), 16), type_str="unknown", size=0)
                )
                continue
            fm = re.match(
                r"\s*\+?\s*0x([0-9a-fA-F]+)\s+(\S+)\s+(\S+)",
                line,
            )
            if fm:
                fields.append(
                    StructField(
                        name=fm.group(3),
                        offset=int(fm.group(1), 16),
                        type_str=fm.group(2),
                        size=0,
                    )
                )

        return StructDef(name=name, size=size, fields=fields)

    # -- enum -----------------------------------------------------------------

    def get_enum(self, name: str) -> EnumDef | None:
        """Retrieve an enum definition by name."""
        raw = self._try_run("source-enum", name)
        if raw is None:
            return None

        # Parse values from lines like:
        #   "VALUE_NAME = 42"
        values: list[EnumValue] = []
        for line in raw.splitlines():
            m = re.match(r"\s*(\w+)\s*=\s*(-?\d+)", line)
            if m:
                values.append(EnumValue(name=m.group(1), value=int(m.group(2))))

        return EnumDef(name=name, values=values)

    # -- asm ------------------------------------------------------------------

    def get_asm(self, target: str) -> AsmResult | None:
        """Retrieve disassembly for a function."""
        raw = self._try_run("asm", target)
        if raw is None:
            return None

        lines = raw.strip().splitlines()
        # Count CALL instructions
        call_count = sum(1 for ln in lines if "CALL" in ln.upper())
        # Check for FP-sensitive instructions
        from re_agent.utils.text import has_fp_asm

        return AsmResult(
            address=target,
            instructions=raw,
            instruction_count=len(lines),
            call_count=call_count,
            has_fp_sensitive=has_fp_asm(raw),
        )

    def _artifact(self, kind: str, command: str, target: str) -> AnalysisArtifact | None:
        raw = self._try_run(command, target)
        if raw is None:
            return None
        return AnalysisArtifact(kind=kind, target=target, content=raw)

    def get_context(self, target: str) -> AnalysisArtifact | None:
        return self._artifact("function-context", "context", target)

    def get_vtable(self, target: str) -> AnalysisArtifact | None:
        return self._artifact("vtable", "vtable", target)

    def get_global(self, target: str) -> AnalysisArtifact | None:
        return self._artifact("global", "global", target)

    def search_strings(self, pattern: str) -> AnalysisArtifact | None:
        return self._artifact("strings", "strings", pattern)

    def get_pcode(self, target: str) -> AnalysisArtifact | None:
        return self._artifact("pcode", "pcode", target)

    def get_cfg(self, target: str) -> AnalysisArtifact | None:
        return self._artifact("cfg", "cfg", target)

    # -- search / unimplemented / remaining -----------------------------------

    def search(self, pattern: str) -> list[FunctionEntry]:
        """Search for functions matching a pattern."""
        raw = self._run("search", pattern)
        return self._parse_function_list(raw)

    def unimplemented(self, filter_pattern: str | None = None) -> list[FunctionEntry]:
        """List unimplemented functions, optionally filtered."""
        args = ["unimplemented"]
        if filter_pattern:
            args.append(filter_pattern)
        raw = self._run(*args)
        return self._parse_function_list(raw)

    def remaining(self, class_name: str | None = None) -> list[FunctionEntry]:
        """List remaining stub functions, optionally filtered by class."""
        args = ["remaining"]
        if class_name:
            args.append(class_name)
        raw = self._run(*args)
        return self._parse_function_list(raw)

    @staticmethod
    def _parse_function_list(raw: str) -> list[FunctionEntry]:
        """Parse function list output into FunctionEntry objects.

        Handles several common formats:
        - ``0xADDRESS  ClassName::FunctionName``
        - ``0xADDRESS  FunctionName  (N callers)``
        - Free-form lines with at least an address token.
        """
        results: list[FunctionEntry] = []
        for line in raw.splitlines():
            line = line.strip()
            if not line or line.startswith(("#", "=", "-", "//")):
                continue

            parts = line.split()
            if not parts:
                continue

            addr = parts[0]
            if not re.fullmatch(r"(?:0x)?[0-9a-fA-F]+", addr):
                continue
            name = parts[1] if len(parts) > 1 else ""
            class_name = ""
            caller_count = 0

            bracketed = re.match(
                r"^(0x[0-9a-fA-F]+)\s+\[\s*(?:(\d+)\s+callers?|no callers?)\]\s+(\S+)",
                line,
            )
            if bracketed:
                addr = bracketed.group(1)
                caller_count = int(bracketed.group(2) or 0)
                name = bracketed.group(3)

            # Split "Class::Func" into class_name and name.
            if "::" in name:
                class_name, _, name = name.rpartition("::")

            # Look for "(N callers)" at the end.
            cm = re.search(r"\((\d+)\s+callers?\)", line)
            if cm:
                caller_count = int(cm.group(1))

            results.append(
                FunctionEntry(
                    address=addr,
                    name=name,
                    class_name=class_name,
                    caller_count=caller_count,
                )
            )
        return results
