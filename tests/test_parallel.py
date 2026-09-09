"""Deterministic scheduler tests without network/model requests."""
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import Mock

import pytest

from re_agent.config.schema import ReAgentConfig
from re_agent.core.models import FunctionTarget, ReversalResult
from re_agent.core.session import Session
from re_agent.orchestrator.execution import current
from re_agent.orchestrator.parallel import reverse_parallel


@pytest.fixture
def setup(tmp_path):
    config = ReAgentConfig()
    config.output.report_dir = str(tmp_path / "reports")
    config.output.session_file = str(tmp_path / "session.json")
    config.project_profile.source_root = str(tmp_path / "source")
    config.orchestrator.max_parallel_functions = 2
    config.orchestrator.max_attempts_per_function = 1
    config.orchestrator.selection_strategy = "high-impact"
    config.validation.enabled = False
    config.parity.enabled = False
    backend = Mock()
    backend.xrefs_from.return_value = []
    targets = [FunctionTarget(hex(i), "C", f"f{i}") for i in range(1, 5)]
    return config, backend, targets, Session(config.output.session_file)


@pytest.mark.parametrize("workers", [1, 2, 4])
def test_bounded_overlap_isolation_order_and_checkpoints(setup, monkeypatch, workers):
    config, backend, targets, session = setup
    config.orchestrator.max_parallel_functions = workers
    barrier = threading.Barrier(workers)
    paths, providers = [], []
    lock = threading.Lock()

    def fake(target, cfg, backend, llm, *, checker_llm, session):
        with lock:
            paths.append(cfg.output.report_dir)
            providers.extend([llm, checker_llm])
        barrier.wait(timeout=10)
        current().emit("call", "reverser")
        result = ReversalResult(target, code="int f() { return 1; }", success=True, rounds_used=1)
        session.record_checkpoint(result)
        return result

    monkeypatch.setattr("re_agent.orchestrator.parallel.reverse_single", fake)
    result = reverse_parallel([*targets, targets[0]], config, backend, session, lambda c: Mock(), 4)
    assert [r.target for r in result] == targets
    assert len(set(paths)) == 4
    assert len({id(p) for p in providers}) == 8
    assert all(p.close.call_count == 1 for p in providers)
    assert all(session.attempt_count(t.address) == 1 for t in targets)
    journal = json.loads(next(Path(config.output.report_dir).rglob("jobs.json")).read_text())
    assert all(j["calls"] == 1 and j["rounds"] == 1 for j in journal.values())
    # Recovery publication is idempotent, including completed jobs.
    assert reverse_parallel(targets, config, backend, session, lambda c: Mock(), 4) == []
    assert all(session.attempt_count(t.address) == 1 for t in targets)


def test_cancel_late_result_and_resume_budget(setup, monkeypatch):
    config, backend, targets, session = setup
    cancel = threading.Event()
    budgets = []

    def interrupted(target, cfg, backend, llm, **kwargs):
        current().emit("call", "reverser")
        current().emit("checkpoint", ReversalResult(target, code="draft", rounds_used=1))
        cancel.set()
        return ReversalResult(target, code="late", success=True)

    monkeypatch.setattr("re_agent.orchestrator.parallel.reverse_single", interrupted)
    assert reverse_parallel(targets[:1], config, backend, session, lambda c: Mock(), 1, cancel=cancel) == []
    assert session.attempt_count(targets[0].address) == 0

    def resumed(target, cfg, backend, llm, **kwargs):
        budgets.append((cfg.orchestrator.max_llm_calls_per_function, cfg.orchestrator.max_review_rounds))
        return ReversalResult(target, code="accepted", success=True, rounds_used=1)

    monkeypatch.setattr("re_agent.orchestrator.parallel.reverse_single", resumed)
    config.orchestrator.max_parallel_functions = 4
    result = reverse_parallel(targets[:1], config, backend, session, lambda c: Mock(), 1)
    assert budgets == [(config.orchestrator.max_llm_calls_per_function - 1,
                        config.orchestrator.max_review_rounds - 1)]
    assert result[0].rounds_used == 2
    assert session.attempt_count(targets[0].address) == 1


def test_retry_limit_and_failure_independence(setup, monkeypatch):
    config, backend, targets, session = setup
    config.orchestrator.max_attempts_per_function = 2

    def fake(target, *args, **kwargs):
        return ReversalResult(target, code="", success=target != targets[0])

    monkeypatch.setattr("re_agent.orchestrator.parallel.reverse_single", fake)
    result = reverse_parallel(targets, config, backend, session, lambda c: Mock(), 5)
    assert len(result) == 5
    assert session.attempt_count(targets[0].address) == 2
    assert all(session.is_completed(t.address) for t in targets[1:])


def test_competing_coordinator_lease(setup, monkeypatch):
    config, backend, targets, session = setup
    entered, release = threading.Event(), threading.Event()

    def fake(target, *args, **kwargs):
        entered.set()
        assert release.wait(10)
        return ReversalResult(target, code="", success=True)

    monkeypatch.setattr("re_agent.orchestrator.parallel.reverse_single", fake)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(reverse_parallel, targets[:1], config, backend, session, lambda c: Mock(), 1)
        assert entered.wait(10)
        try:
            with pytest.raises(OSError):
                reverse_parallel(targets[:1], config, backend, session, lambda c: Mock(), 1)
        finally:
            release.set()
        assert len(first.result(timeout=10)) == 1
