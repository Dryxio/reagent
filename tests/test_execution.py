import sys
import threading
from concurrent.futures import ThreadPoolExecutor

import pytest

from re_agent.orchestrator.execution import Cancelled, Execution, executing, validation_lane
from re_agent.utils.process import run_process


def test_cancelled_process_and_context_cleanup():
    cancel = threading.Event()
    context = Execution(cancel, threading.Semaphore(1), lambda *_: None)
    cancel.set()
    with pytest.raises(Cancelled), executing(context):
        pass
    # An unrelated call outside the scope is not cancelled.
    assert run_process([sys.executable, "-c", "print('ok')"]).stdout.strip() == "ok"


def test_validation_wait_is_cancellable():
    cancel = threading.Event()
    waiting = threading.Event()
    semaphore = threading.Semaphore(0)
    context = Execution(cancel, semaphore, lambda *_: waiting.set())

    def worker():
        with executing(context), validation_lane():
            pytest.fail("Validation must not run")

    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(worker)
        assert waiting.wait(5)
        cancel.set()
        with pytest.raises(Cancelled):
            future.result(timeout=5)
