"""Persistent binary knowledge graph built from backend evidence bundles."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from re_agent.core.models import EvidenceGap
from re_agent.utils.address import normalize_address
from re_agent.utils.evidence import bounded_evidence
from re_agent.utils.storage import atomic_json


class KnowledgeGraph:
    """Small JSON-backed graph of functions, globals, strings, and calls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, str]] = []
        self.contexts: dict[str, dict[str, Any]] = {}
        self.gaps: list[EvidenceGap] = []
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.nodes = payload.get("nodes", {})
                self.edges = payload.get("edges", [])
                self.contexts = payload.get("contexts", {})
                self.gaps = [EvidenceGap.from_dict(gap) for gap in payload.get("gaps", [])]
            except (json.JSONDecodeError, OSError, AttributeError):
                pass

    def ingest_context(self, content: str) -> None:
        try:
            payload = json.loads(content)
        except json.JSONDecodeError:
            return
        if not isinstance(payload, dict) or payload.get("kind") != "function-context":
            return
        function = payload.get("function", {})
        if not isinstance(function, dict):
            return
        address = normalize_address(str(function.get("address") or payload.get("target") or ""))
        if not address:
            return
        raw_gaps = payload.get("gaps", [])
        if not isinstance(raw_gaps, list):
            raise ValueError("Context gaps must be a list")
        gaps = [EvidenceGap.from_dict(gap) for gap in raw_gaps]
        if any(gap.function != address for gap in gaps):
            raise ValueError("Context gap function does not match context address")
        self.contexts[address] = payload
        # A refreshed context replaces that function's earlier gap observations.
        self.gaps = [gap for gap in self.gaps if gap.function != address] + gaps
        source = self._put("function", address, function)
        self.edges = [edge for edge in self.edges if edge.get("source") != source]

        for callee in function.get("callees", []):
            if not isinstance(callee, dict):
                continue
            raw_target = str(callee.get("addr", ""))
            target = normalize_address(raw_target) if raw_target else ""
            if target:
                self._edge(source, self._put("function", target, callee), "calls")
        for item in payload.get("strings", []):
            if isinstance(item, dict):
                target = str(item.get("address", item.get("value", "")))
                self._edge(source, self._put("string", target, item), "references")
        for item in payload.get("globals", []):
            if isinstance(item, dict):
                target = str(item.get("address", item.get("name", "")))
                self._edge(source, self._put("global", target, item), "accesses")
        self.save()

    def neighborhood(self, address: str, max_chars: int = 6000) -> str:
        root = f"function:{normalize_address(address)}"
        related = [edge for edge in self.edges if edge.get("source") == root or edge.get("target") == root]
        node_ids = {root}
        for edge in related:
            node_ids.add(edge.get("source", ""))
            node_ids.add(edge.get("target", ""))
        payload = {
            "nodes": {node_id: self.nodes[node_id] for node_id in node_ids if node_id in self.nodes},
            "edges": related,
            "gaps": [asdict(gap) for gap in self.gaps if f"function:{gap.function}" in node_ids],
        }
        return bounded_evidence(json.dumps(payload, indent=2), max_chars)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "nodes": self.nodes, "edges": self.edges,
                   "contexts": self.contexts, "gaps": [asdict(gap) for gap in self.gaps]}
        atomic_json(self.path, payload)

    def _put(self, kind: str, identity: str, data: dict[str, Any]) -> str:
        node_id = f"{kind}:{identity}"
        current = self.nodes.get(node_id, {})
        self.nodes[node_id] = {**current, **data, "kind": kind}
        return node_id

    def _edge(self, source: str, target: str, relation: str) -> None:
        edge = {"source": source, "target": target, "relation": relation}
        if edge not in self.edges:
            self.edges.append(edge)
