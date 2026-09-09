"""Deterministic, bounded target inventories built without model calls."""
from __future__ import annotations

import json
from collections import deque
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from re_agent.backend.protocol import REBackend
from re_agent.core.models import EvidenceGap, FunctionTarget
from re_agent.utils.address import normalize_address
from re_agent.utils.storage import atomic_json


def checked_address(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("Target address must be a hexadecimal string")
    result = normalize_address(value)
    if not all(char in "0123456789abcdef" for char in result):
        raise ValueError(f"Invalid hexadecimal address: {value}")
    return result


@dataclass
class TargetPlan:
    identity: str
    seeds: list[str]
    functions: list[FunctionTarget] = field(default_factory=list)
    edges: list[dict[str, str]] = field(default_factory=list)
    gaps: list[EvidenceGap] = field(default_factory=list)
    evidence: dict[str, dict[str, Any]] = field(default_factory=dict)
    max_depth: int = 1
    max_functions: int = 100

    def save(self, path: Path) -> None:
        atomic_json(path, {"schema_version": 1, **asdict(self)})

    @classmethod
    def load(cls, path: Path) -> TargetPlan:
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, dict) or value.get("schema_version") != 1:
            raise ValueError("Unsupported target manifest schema")
        identity = value.get("identity")
        if not isinstance(identity, str) or len(identity) != 64 or any(c not in "0123456789abcdef" for c in identity):
            raise ValueError("Target manifest requires a project fingerprint")
        for key in ("seeds", "functions", "edges", "gaps"):
            if not isinstance(value.get(key), list):
                raise ValueError(f"Target manifest {key} must be a list")
        depth, limit = value.get("max_depth"), value.get("max_functions")
        if not isinstance(depth, int) or not isinstance(limit, int):
            raise ValueError("Manifest limits must be integers")
        _limits(depth, limit)
        plan = cls(identity, [checked_address(seed) for seed in value["seeds"]],
                   max_depth=depth, max_functions=limit)
        seen = set()
        for item in value["functions"]:
            if not isinstance(item, dict):
                raise ValueError("Target manifest function must be an object")
            address = checked_address(item.get("address"))
            if address in seen:
                raise ValueError(f"Duplicate manifest function: {address}")
            seen.add(address)
            if any(not isinstance(item.get(key), str) for key in ("class_name", "function_name")):
                raise ValueError("Target names must be strings")
            count = item.get("caller_count", 0)
            if type(count) is not int or count < 0:
                raise ValueError("Target caller_count must be nonnegative")
            plan.functions.append(FunctionTarget(address, item["class_name"], item["function_name"], count))
        if not plan.functions or len(plan.functions) > limit:
            raise ValueError("Target manifest must contain functions within its declared limit")
        for edge in value["edges"]:
            if not isinstance(edge, dict):
                raise ValueError("Target edge must be an object")
            source, target = checked_address(edge.get("source")), checked_address(edge.get("target"))
            if source not in seen:
                raise ValueError("Target edge source is outside manifest")
            plan.edges.append({"source": source, "target": target})
        plan.gaps = [EvidenceGap.from_dict(gap) for gap in value["gaps"]]
        evidence = value.get("evidence", {})
        if not isinstance(evidence, dict) or any(
            checked_address(key) not in seen or not isinstance(record, dict) for key, record in evidence.items()
        ):
            raise ValueError("Manifest evidence must map selected addresses to objects")
        plan.evidence = {checked_address(key): record for key, record in evidence.items()}
        return plan


def _limits(depth: int, limit: int) -> None:
    if type(depth) is not int or depth < 0 or type(limit) is not int or limit < 1:
        raise ValueError("Plan depth must be nonnegative and function limit must be positive")


def build_plan(backend: REBackend, seeds: list[str], identity: str, *,
               max_depth: int = 1, max_functions: int = 100) -> TargetPlan:
    _limits(max_depth, max_functions)
    roots = sorted({checked_address(seed) for seed in seeds})
    if not roots:
        raise ValueError("Planning requires at least one seed address or search result")
    plan = TargetPlan(identity, roots, max_depth=max_depth, max_functions=max_functions)
    queue = deque((root, 0) for root in roots)
    queued = set(roots)
    selected = set()
    while queue and len(plan.functions) < max_functions:
        address, depth = queue.popleft()
        selected.add(address)
        target = FunctionTarget(address, "", f"FUN_{address}")
        record: dict[str, Any] = {"depth": depth, "selection": "seed" if address in roots else "direct callee"}
        try:
            dec = backend.decompile(address)
            scope, _, name = dec.name.rpartition("::")
            target.class_name, target.function_name = scope, name or dec.name or target.function_name
            target.caller_count = dec.callers or 0
            record["decompile"] = asdict(dec)
        except (OSError, ValueError, RuntimeError, NotImplementedError) as exc:
            plan.gaps.append(EvidenceGap(address, str(exc), "decompile", "query_failed"))
        plan.functions.append(target)
        plan.evidence[address] = record
        if backend.capabilities.has_context:
            try:
                method = getattr(backend, "get_context", None)
                if not callable(method):
                    raise ValueError("Backend advertises context without implementing get_context")
                context = method(address)
                if context is None:
                    plan.gaps.append(EvidenceGap(address, "No context returned", "context", "unavailable"))
                else:
                    payload = json.loads(context.content)
                    if not isinstance(payload, dict):
                        raise ValueError("Context must be an object")
                    record["context"] = payload
                    plan.gaps.extend(EvidenceGap.from_dict(gap) for gap in payload.get("gaps", []))
            except (OSError, ValueError, RuntimeError, NotImplementedError, AttributeError) as exc:
                plan.gaps.append(EvidenceGap(address, str(exc), "context", "query_failed"))
        else:
            plan.gaps.append(EvidenceGap(address, "Backend does not provide context", "context", "unsupported"))
        if not backend.capabilities.has_xrefs:
            plan.gaps.append(EvidenceGap(address, "Backend does not provide cross-references", "xrefs", "unsupported"))
            continue
        try:
            refs = backend.xrefs_from(address)
            callees = sorted({checked_address(ref.address) for ref in refs if "CALL" in ref.ref_type.upper()})
        except (OSError, ValueError, RuntimeError, NotImplementedError) as exc:
            plan.gaps.append(EvidenceGap(address, str(exc), "xrefs_from", "query_failed"))
            continue
        for callee in callees:
            plan.edges.append({"source": address, "target": callee})
            if callee not in queued and depth < max_depth:
                queued.add(callee)
                queue.append((callee, depth + 1))
    for edge in plan.edges:
        if edge["target"] not in selected:
            plan.gaps.append(EvidenceGap(edge["source"], f"Dependency {edge['target']} outside bounded selection",
                                         "target-plan", "limit"))
    for root in roots:
        if root not in selected:
            plan.gaps.append(EvidenceGap(root, "Seed omitted by function limit", "target-plan", "limit"))
    return plan
