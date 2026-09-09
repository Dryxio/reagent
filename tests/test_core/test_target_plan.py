import json
from pathlib import Path
from unittest.mock import patch

import pytest

from re_agent.backend.stub import StubBackend
from re_agent.cli.main import main
from re_agent.core.models import XRef
from re_agent.core.target_plan import TargetPlan, build_plan


class GraphBackend(StubBackend):
    def xrefs_from(self, target: str) -> list[XRef]:
        edges = {"140001000": ["140002000", "140003000"], "140002000": ["140001000"]}
        return [XRef(address, "", "CALL") for address in edges.get(target, [])]


def test_bounded_cycle_and_roundtrip(tmp_path: Path) -> None:
    plan = build_plan(GraphBackend(), ["0x140001000", "140001000"], "a" * 64, max_depth=3, max_functions=2)
    assert [target.address for target in plan.functions] == ["140001000", "140002000"]
    assert any("140003000" in gap.reason for gap in plan.gaps)
    path = tmp_path / "manifest.json"
    plan.save(path)
    assert TargetPlan.load(path) == plan
    previous = path.read_bytes()
    build_plan(GraphBackend(), ["140001000"], "a" * 64, max_depth=3, max_functions=2).save(path)
    assert path.read_bytes() == previous


def test_depth_zero_reports_external_dependencies() -> None:
    plan = build_plan(GraphBackend(), ["140001000"], "a" * 64, max_depth=0)
    assert len(plan.functions) == 1
    assert len([gap for gap in plan.gaps if gap.kind == "limit"]) == 2


def test_query_failure_remains_visible() -> None:
    class Broken(StubBackend):
        def xrefs_from(self, target: str) -> list[XRef]:
            raise RuntimeError("evidence unavailable")
    plan = build_plan(Broken(), ["100"], "a" * 64)
    assert any(gap.kind == "query_failed" and gap.origin == "xrefs_from" for gap in plan.gaps)


@pytest.mark.parametrize("method, origin", [
    ("decompile", "decompile"), ("get_context", "context"), ("xrefs_from", "xrefs_from"),
])
@pytest.mark.parametrize("message", ["", " \t\n", "evidence unavailable"])
def test_query_failure_manifest_remains_loadable(
    tmp_path: Path, method: str, origin: str, message: str,
) -> None:
    backend = StubBackend()
    with patch.object(backend, method, side_effect=NotImplementedError(message)):
        plan = build_plan(backend, ["100"], "a" * 64)
    path = tmp_path / "manifest.json"
    plan.save(path)

    restored = TargetPlan.load(path)
    assert restored == plan
    gap = next(gap for gap in restored.gaps if gap.origin == origin)
    assert gap.kind == "query_failed"
    assert gap.reason.strip()
    if message.strip():
        assert gap.reason == message


@pytest.mark.parametrize("depth, limit", [(-1, 1), (0, 0)])
def test_invalid_limits(depth: int, limit: int) -> None:
    with pytest.raises(ValueError):
        build_plan(StubBackend(), ["100"], "a" * 64, max_depth=depth, max_functions=limit)


def test_plan_cli_never_creates_provider(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text("backend:\n  type: stub\n", encoding="utf-8")
    path = tmp_path / "plan.json"
    with patch("re_agent.llm.registry.create_provider", side_effect=AssertionError("No LLM calls")):
        assert main(["--config", str(config), "plan", "--address", "0x100", "--output", str(path)]) == 0
    assert TargetPlan.load(path).functions[0].address == "00000100"


def test_duplicate_manifest_targets_rejected(tmp_path: Path) -> None:
    path = tmp_path / "plan.json"
    build_plan(StubBackend(), ["100"], "a" * 64).save(path)
    data = json.loads(path.read_text())
    data["functions"].append(data["functions"][0])
    path.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError, match="Duplicate"):
        TargetPlan.load(path)
