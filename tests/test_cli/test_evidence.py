import json
from pathlib import Path
from unittest.mock import patch

import pytest

from re_agent.backend.stub import StubBackend
from re_agent.cli.main import main
from re_agent.core.models import EvidenceGap
from re_agent.core.target_plan import build_plan
from re_agent.reports.evidence import export_evidence


def test_evidence_is_deterministic_complete_and_linked(tmp_path: Path) -> None:
    plan = build_plan(StubBackend(), ["140001000"], "a" * 64)
    plan.functions[0].function_name = "name\twith\nnewlines"
    plan.gaps.append(EvidenceGap("140001000", "limit\tobserved", "fixture", "limit"))
    plan.evidence["140001000"]["context"] = {"truncated": True, "strings": [{"value": "abc\nxyz"}]}
    for directory in (tmp_path / "a", tmp_path / "b"):
        export_evidence(plan, directory)
    files = sorted(path.relative_to(tmp_path / "a") for path in (tmp_path / "a").rglob("*") if path.is_file())
    for file in files:
        assert (tmp_path / "a" / file).read_bytes() == (tmp_path / "b" / file).read_bytes()
    row = (tmp_path / "a/functions.tsv").read_text().splitlines()[1].split("\t")
    assert len(row) == 5 and row[2] == r"name\twith\nnewlines"
    packet = json.loads((tmp_path / "a" / row[4]).read_text())
    assert packet["stored_evidence"]["context"]["truncated"] is True
    assert packet["gaps"][-1]["reason"] == "limit\tobserved"
    with pytest.raises(ValueError, match="empty directory"):
        export_evidence(plan, tmp_path / "a")


def test_export_requires_no_provider_or_backend(tmp_path: Path) -> None:
    plan = build_plan(StubBackend(), ["100"], "a" * 64)
    path = tmp_path / "plan.json"
    plan.save(path)
    with patch("re_agent.llm.registry.create_provider", side_effect=AssertionError), \
         patch("re_agent.backend.registry.create_backend", side_effect=AssertionError):
        assert main(["evidence", "--manifest", str(path), "--output", str(tmp_path / "out")]) == 0
