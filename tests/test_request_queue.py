"""Real thread overlap, cancellation, and explicit request-budget tests."""
import threading
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock

import pytest

from re_agent.llm.observed import CallBudget, ObservedProvider
from re_agent.llm.protocol import Message
from re_agent.orchestrator.execution import Cancelled, Execution, RequestQueue, executing


def test_32_functions_share_four_request_slots():
    queue = RequestQueue(4)
    barrier = threading.Barrier(4)
    lock = threading.Lock()
    active = peak = 0

    def send(*args, **kwargs):
        nonlocal active, peak
        with lock:
            active += 1
            peak = max(peak, active)
        barrier.wait(10)
        with lock:
            active -= 1
        return "ok"

    def job(_):
        context = Execution(threading.Event(), threading.Semaphore(1), lambda *args: None, queue)
        with executing(context):
            budget = CallBudget(1)
            assert ObservedProvider(Mock(send=send), budget, "reverser", None).send([Message("user", "test")]) == "ok"
            assert budget.used == 1

    with ThreadPoolExecutor(max_workers=32) as pool:
        list(pool.map(job, range(32)))
    assert peak == 4
    assert queue.snapshot() == {"limit": 4, "active": 0, "queued": 0}


def test_cancel_queued_request_does_not_spend_budget():
    queue = RequestQueue(1)
    cancel = threading.Event()
    context = Execution(cancel, threading.Semaphore(1), lambda *args: None, queue)
    queued = threading.Event()
    context.emit = lambda *args: queued.set()
    provider, budget = Mock(), CallBudget(1)

    def waiting():
        with executing(context):
            ObservedProvider(provider, budget, "checker", None).send([])

    with (queue.acquire(Execution(threading.Event(), threading.Semaphore(1), lambda *args: None)),
          ThreadPoolExecutor() as pool):
        future = pool.submit(waiting)
        assert queued.wait(10)
        cancel.set()
        with pytest.raises(Cancelled):
            future.result(timeout=10)
    assert budget.used == 0
    provider.send.assert_not_called()
    assert queue.snapshot()["queued"] == 0


class RateLimit(Exception):
    status_code = 429


@pytest.mark.parametrize("limit,expected_calls", [(3, 3), (2, 2)])
def test_rate_limit_retries_spend_budget_and_release_slots(monkeypatch, limit, expected_calls):
    queue = RequestQueue(1)
    cancel = threading.Event()
    # Skip real backoff in this test; still verify the shared cooldown is set.
    monkeypatch.setattr(cancel, "wait", lambda seconds: False)
    monkeypatch.setattr(queue, "defer", Mock())
    events = []
    context = Execution(cancel, threading.Semaphore(1), lambda *args: events.append(args), queue, 2)
    provider = Mock(send=Mock(side_effect=[RateLimit(), RateLimit(), "ok"]))
    budget = CallBudget(limit)
    with executing(context):
        observed = ObservedProvider(provider, budget, "reverser", None)
        if limit == 3:
            assert observed.send([]) == "ok"
        else:
            with pytest.raises(RuntimeError, match="budget exhausted"):
                observed.send([])
    assert provider.send.call_count == expected_calls
    assert budget.used == expected_calls
    assert queue.snapshot()["active"] == 0
    assert queue.defer.call_count == 2
    assert len([e for e in events if e[0] == "request_timing"]) == expected_calls


def test_failure_is_not_retried_and_slot_is_released():
    queue = RequestQueue(1)
    context = Execution(threading.Event(), threading.Semaphore(1), lambda *args: None, queue, 3)
    provider = Mock(send=Mock(side_effect=ValueError("invalid output")))
    with executing(context), pytest.raises(ValueError):
        ObservedProvider(provider, CallBudget(4), "checker", None).send([])
    assert provider.send.call_count == 1
    assert queue.snapshot()["active"] == 0
