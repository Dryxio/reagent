"""Typed reader for local Ghidra JSON exports; no display-output parsing."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from re_agent.backend.protocol import BackendCapabilities
from re_agent.backend.stub import StubBackend
from re_agent.core.models import (
    AnalysisArtifact,
    AsmResult,
    DecompileResult,
    FunctionEntry,
    StructDef,
    StructField,
    XRef,
)
from re_agent.utils.address import normalize_address
from re_agent.utils.text import has_fp_asm


class GhidraExportsBackend(StubBackend):
    """Consume export schema 1 (including pre-versioned bridge exports).

    Binary identity is the export directory plus its content fingerprint at the
    application layer. Missing mandatory fields fail rather than becoming evidence.
    """

    def __init__(self, export_dir: str, address_map: str | None = None) -> None:
        super().__init__()
        self.root = Path(export_dir).resolve()
        if not self.root.is_dir():
            raise ValueError(f"Export directory not found: {self.root}")
        self.names = self._json(Path(address_map)) if address_map else {}
        self.index = self._json(self.root / "_index.json")
        self._caps = BackendCapabilities(
            has_decompile=True,
            has_xrefs=True,
            has_structs=True,
            has_context=True,
            has_pcode=True,
            has_cfg=True,
            has_asm=True,
            has_search=True,
        )

    @staticmethod
    def _json(path: Path) -> dict[str, Any]:
        if not path.exists():
            return {}
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version", 1) != 1:
            raise ValueError(f"Unsupported Ghidra JSON schema in {path}")
        return value

    def _function(self, target: str) -> dict[str, Any]:
        key = normalize_address(target)
        if not all(c in "0123456789abcdef" for c in key):
            raise ValueError("JSON backend requires a hexadecimal function address")
        value = self._json(self.root / f"{key}.json")
        if not value or normalize_address(str(value.get("address", ""))) != key:
            raise ValueError(f"Function not found or address mismatch: {target}")
        return value

    def _name(self, address: str, fallback: str) -> str:
        key = normalize_address(address)
        entry = self.names.get(key, self.names.get(key.lstrip("0"), {}))
        return str(entry.get("full_name") or fallback)

    def decompile(self, target: str) -> DecompileResult:
        value = self._function(target)
        code = value.get("decompiled")
        if not isinstance(code, str) or not code.strip():
            raise ValueError(f"Missing decompiled evidence for {target}")
        name = self._name(target, str(value.get("name", target)))
        return DecompileResult(
            target,
            name,
            str(value.get("signature", "")),
            code,
            code,
            len(self.xrefs_to(target)),
            len(self.xrefs_from(target)),
        )

    def _refs(self, target: str, key: str) -> list[XRef]:
        refs = self._function(target).get(key, [])
        if not isinstance(refs, list):
            raise ValueError(f"Invalid {key} in {target}")
        out = []
        for ref in refs:
            if not isinstance(ref, dict) or not isinstance(ref.get("addr"), str):
                raise ValueError(f"Invalid reference in {target}")
            addr = ref["addr"]
            out.append(XRef(addr, self._name(addr, str(ref.get("name", ""))), str(ref.get("ref_type", "CALL"))))
        return out

    def xrefs_from(self, target: str) -> list[XRef]:
        return self._refs(target, "callees")

    def xrefs_to(self, target: str) -> list[XRef]:
        return self._refs(target, "callers")

    def get_struct(self, name: str) -> StructDef | None:
        value = self._json(self.root / "_source_structs.json").get(name)
        if not isinstance(value, dict):
            return None
        fields = [
            StructField(
                str(f["field"]),
                int(f.get("offset_dec", int(f.get("offset_hex", "0"), 16))),
                str(f.get("type", "unknown")),
                int(f.get("size", 0)),
            )
            for f in value.get("fields", [])
        ]
        return StructDef(name, int(value.get("size_dec", 0)), fields)

    def get_context(self, target: str) -> AnalysisArtifact:
        value = self._function(target)
        data = {
            "schema_version": 1,
            "kind": "function-context",
            "target": target,
            "function": {
                k: value.get(k) for k in ("address", "name", "signature", "calling_convention", "callees", "callers")
            },
            "globals": value.get("data_refs", []),
            "strings": [],
            "cfg": value.get("cfg", []),
        }
        return AnalysisArtifact("function-context", target, json.dumps(data))

    def _ir(self, target: str, kind: str) -> AnalysisArtifact | None:
        value = self._function(target)
        data = value.get(kind)
        if not data or value.get(kind + "_errors") or value.get("ir_errors"):
            return None
        if not isinstance(data, list):
            raise ValueError(f"Invalid {kind} evidence for {target}")
        return AnalysisArtifact(kind, target, json.dumps({"schema_version": 1, "data": data}))

    def get_pcode(self, target: str) -> AnalysisArtifact | None:
        return self._ir(target, "pcode")

    def get_cfg(self, target: str) -> AnalysisArtifact | None:
        return self._ir(target, "cfg")

    def get_asm(self, target: str) -> AsmResult | None:
        value = self._function(target).get("assembly", [])
        if not value:
            return None
        lines = value if isinstance(value, list) else str(value).splitlines()
        text = "\n".join(str(line) for line in lines)
        import re

        calls = sum(bool(re.search(r"\b(?:CALL|BL|BLR)\b", str(line), re.I)) for line in lines)
        return AsmResult(target, text, len(lines), calls, has_fp_asm(text))

    def search(self, pattern: str) -> list[FunctionEntry]:
        entries = []
        for address, data in self.index.items():
            if not isinstance(data, dict):
                continue
            name = self._name(address, str(data.get("name", address)))
            if pattern.lower() not in name.lower():
                continue
            cls, _, fn = name.rpartition("::")
            entries.append(FunctionEntry(address, fn or name, cls))
        return entries

    def remaining(self, class_name: str | None = None) -> list[FunctionEntry]:
        # Explicit class enumeration has no text-display limit. Session state
        # determines which exported functions have already been accepted.
        return self.search(class_name or "")

    def unimplemented(self, filter_pattern: str | None = None) -> list[FunctionEntry]:
        return self.search(filter_pattern or "")
