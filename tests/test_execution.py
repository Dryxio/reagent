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


def test_cancel_reaps_detached_process_tree(tmp_path):
    import json
    import os
    import sys
    import time
    from concurrent.futures import ThreadPoolExecutor

    import psutil
    import pytest

    from re_agent.utils.process import run_process

    ready = tmp_path / "ready.json"
    script = tmp_path / "parent.py"
    script.write_text(
        "import subprocess, sys, pathlib, json, os, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'], "
        "start_new_session=os.name != 'nt')\n"
        "pathlib.Path(sys.argv[1]).write_text(json.dumps([os.getpid(), child.pid]))\n"
        "time.sleep(60)\n"
    )
    cancel = threading.Event()

    def invoke():
        with executing(Execution(cancel, threading.Semaphore(1), lambda *args: None)):
            run_process([sys.executable, str(script), str(ready)], timeout_s=20)

    with ThreadPoolExecutor() as pool:
        future = pool.submit(invoke)
        deadline = time.monotonic() + 10
        while not ready.exists() and time.monotonic() < deadline:
            time.sleep(.02)
        try:
            assert ready.exists()
            pids = json.loads(ready.read_text())
        finally:
            cancel.set()
        with pytest.raises(Cancelled):
            future.result(timeout=10)
    for pid in pids:
        if psutil.pid_exists(pid):
            # POSIX init may not have reaped an already-dead grandchild yet.
            assert os.name != "nt" and psutil.Process(pid).status() == psutil.STATUS_ZOMBIE
