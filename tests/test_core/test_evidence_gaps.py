import json
from pathlib import Path

import pytest

from re_agent.backend.exports import GhidraExportsBackend
from re_agent.core.knowledge_graph import KnowledgeGraph
from re_agent.core.models import EvidenceGap


def test_gap_roundtrip_and_refresh(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    context = {"kind": "function-context", "function": {"address": "0x140001000", "callees": []},
               "gaps": [{"function": "0x140001000", "site": "0x140001010", "kind": "unresolved_call",
                         "origin": "fixture", "reason": "indirect register call"}]}
    graph = KnowledgeGraph(path)
    graph.ingest_context(json.dumps(context))
    restored = KnowledgeGraph(path)
    assert restored.gaps[0].site == "140001010"
    assert not restored.edges
    assert "indirect register call" in restored.neighborhood("0x140001000")
    assert restored.contexts["140001000"] == context
    context["gaps"] = []
    restored.ingest_context(json.dumps(context))
    assert KnowledgeGraph(path).gaps == []


def test_old_graph_loads(tmp_path: Path) -> None:
    path = tmp_path / "graph.json"
    path.write_text('{"schema_version":1,"nodes":{},"edges":[]}', encoding="utf-8")
    assert KnowledgeGraph(path).gaps == []


def test_missing_and_empty_export_fields_are_distinct(tmp_path: Path) -> None:
    path = tmp_path / "140001000.json"
    path.write_text(json.dumps({"address": "140001000", "callees": []}), encoding="utf-8")
    context = json.loads(GhidraExportsBackend(str(tmp_path)).get_context("0x140001000").content)
    assert [gap["reason"] for gap in context["gaps"]] == ["Export does not contain callers"]


@pytest.mark.parametrize("update", [{"kind": "guess"}, {"function": "garbage"}, {"site": 42}, {"reason": ""}])
def test_invalid_gap_rejected(update: dict) -> None:
    gap = {"function": "0x100", "kind": "unavailable", "reason": "missing", "origin": "fixture"}
    gap.update(update)
    with pytest.raises(ValueError):
        EvidenceGap.from_dict(gap)
