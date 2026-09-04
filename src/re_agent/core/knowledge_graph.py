"""Persistent binary knowledge graph built from backend evidence bundles."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from re_agent.utils.address import normalize_address
from re_agent.utils.evidence import bounded_evidence
from re_agent.utils.storage import atomic_json


class KnowledgeGraph:
    """Small JSON-backed graph of functions, globals, strings, and calls."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: list[dict[str, str]] = []
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                self.nodes = payload.get("nodes", {})
                self.edges = payload.get("edges", [])
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
        source = self._put("function", address, function)

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
        }
        return bounded_evidence(json.dumps(payload, indent=2), max_chars)

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {"schema_version": 1, "nodes": self.nodes, "edges": self.edges}
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
