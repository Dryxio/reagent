"""Exercise monitor accounting, HTTP boundaries and real disposable worker trees."""
from __future__ import annotations

import json
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

import psutil
import pytest

from re_agent.cli.main import build_parser
from re_agent.monitor.server import Monitor, make_server


def save(path, functions):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"functions": functions}), encoding="utf-8")


def test_sessions_count_latest_result_once_and_tolerate_partial_writes(tmp_path):
    a, b = tmp_path / "a.json", tmp_path / "b.json"
    save(a, {"f": {"address": "0x00100", "success": False, "rounds_used": 3, "timestamp": "1"}})
    save(b, {"f": {"address": "100", "success": True, "rounds_used": 1, "timestamp": "2"},
             "g": {"address": "200", "success": False, "rounds_used": 2}, "malformed": []})
    monitor = Monitor(tmp_path, tmp_path / "state", ["*.json", "a.json"])
    state = monitor.snapshot()
    assert (state["completed"], state["passed"], state["failed"], state["rounds"]) == (2, 1, 1, 3)
    assert state["phase"] == "read-only"
    assert not state["controls"]
    b.write_text('{"functions":', encoding="utf-8")
    assert monitor.snapshot()["completed"] == 2
    b.unlink()
    assert monitor.snapshot()["passed"] == 0
    assert monitor.snapshot()["completed"] == 1


def test_log_tail_and_recent_results_are_bounded(tmp_path):
    (tmp_path / "activity.log").write_bytes(b"x" * 20000 + b"END")
    save(tmp_path / "session.json", {str(n): {"address": hex(n), "success": True} for n in range(30)})
    state = Monitor(tmp_path, tmp_path / "state", ["session.json"], "*.log").snapshot()
    assert len(state["log"]) == 7000
    assert state["log"].endswith("END")
    assert state["completed"] == 30
    assert len(state["recent"]) == 18


@pytest.mark.parametrize("pattern", ["../*.json", "/absolute/session.json"])
def test_patterns_cannot_escape_work_dir(tmp_path, pattern):
    with pytest.raises(ValueError, match="relative"):
        Monitor(tmp_path, tmp_path / "state", [pattern])


def test_read_only_monitor_cannot_start_or_stop(tmp_path):
    monitor = Monitor(tmp_path, tmp_path / "state", [])
    for action in (monitor.start, monitor.stop):
        with pytest.raises(ValueError, match="Read-only"):
            action()
    assert not (tmp_path / "state").exists()


def test_worker_tree_stop_duplicate_start_and_host_reconnect(tmp_path):
    script = tmp_path / "worker with spaces.py"
    script.write_text(
        "import pathlib, subprocess, sys, time\n"
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(120)'])\n"
        "pathlib.Path('child.pid').write_text(str(child.pid))\n"
        "time.sleep(120)\n", encoding="utf-8")
    command = [sys.executable, str(script)]
    first = Monitor(tmp_path, tmp_path / "state", [], worker=command)
    second = None
    try:
        first.start()
        pid = first.process.pid
        assert first.start()["pid"] == pid
        second = Monitor(tmp_path, tmp_path / "state", [], worker=command)
        assert second.active()
        assert second.start()["pid"] == pid
        deadline = time.monotonic() + 10
        while not (tmp_path / "child.pid").exists() and time.monotonic() < deadline:
            time.sleep(.02)
        child = psutil.Process(int((tmp_path / "child.pid").read_text()))
        second.stop()
        assert not second.active()
        assert not first.active()
        assert first.snapshot()["phase"] == "stopped"
        assert Monitor(tmp_path, tmp_path / "state", [], worker=command).snapshot()["phase"] == "stopped"
        deadline = time.monotonic() + 5
        while child.is_running() and child.status() != psutil.STATUS_ZOMBIE and time.monotonic() < deadline:
            time.sleep(.02)
        assert not child.is_running() or child.status() == psutil.STATUS_ZOMBIE
        first.start()
        assert first.active()
        assert second.snapshot()["phase"] == "running"
        first.stop()
    finally:
        first.stop()
        if second:
            second.stop()


def test_stale_identity_and_unrelated_process_are_not_adopted(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    current = psutil.Process()
    worker = [sys.executable, "not-the-running-worker.py"]
    record = {"pid": current.pid, "created": current.create_time(), "command": worker, "cwd": str(tmp_path)}
    (state / "worker.json").write_text(json.dumps(record))
    monitor = Monitor(tmp_path, state, [], worker=worker)
    assert not monitor.active()
    monitor.stop()
    assert current.is_running()
    record.update(created=0, command=current.cmdline())
    (state / "worker.json").write_text(json.dumps(record))
    assert not Monitor(tmp_path, state, [], worker=current.cmdline()).active()


def test_natural_exit_is_distinct_from_explicit_stop(tmp_path):
    command = [sys.executable, "-c", "import time; time.sleep(.2)"]
    monitor = Monitor(tmp_path, tmp_path / "state", [], worker=command)
    assert monitor.snapshot()["phase"] == "idle"
    monitor.start()
    monitor.process.wait(timeout=10)
    assert monitor.snapshot()["phase"] == "exited"
    monitor.stop()
    assert Monitor(tmp_path, tmp_path / "state", [], worker=command).snapshot()["phase"] == "exited"


def test_adoption_uses_launch_marker_even_if_launcher_changes_argv(tmp_path):
    command = [sys.executable, "-c", "import time; time.sleep(120)"]
    first = Monitor(tmp_path, tmp_path / "state", [], worker=command)
    try:
        first.start()
        saved = json.loads(first.record.read_text())
        saved["process_command"] = ["a-transient-launcher", "different-argv"]
        first.record.write_text(json.dumps(saved))
        adopted = Monitor(tmp_path, tmp_path / "state", [], worker=command)
        assert adopted.active()
        saved["launch_marker"] = "unrelated-launch"
        first.record.write_text(json.dumps(saved))
        assert not Monitor(tmp_path, tmp_path / "state", [], worker=command).active()
    finally:
        first.stop()


@pytest.fixture
def http_monitor(tmp_path):
    monitor = Monitor(tmp_path, tmp_path / "state", [])
    server = make_server(monitor, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield monitor, f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_http_serves_packaged_ui_and_read_only_status(http_monitor):
    _, url = http_monitor
    with urllib.request.urlopen(url) as response:
        html = response.read().decode()
        assert "Reconstruction, live." in html
        assert "__TOKEN__" not in html
        assert response.headers["Cache-Control"] == "no-store"
    with urllib.request.urlopen(url + "/api/status") as response:
        state = json.load(response)
        assert not state["controls"]


@pytest.mark.parametrize("headers", [{}, {"X-Control-Token": "wrong"}, {"Host": "untrusted.example"}])
def test_http_rejects_unauthorized_control(http_monitor, headers):
    _, url = http_monitor
    req = urllib.request.Request(url + "/api/start", method="POST", headers=headers)
    with pytest.raises(urllib.error.HTTPError) as error:
        urllib.request.urlopen(req)
    assert error.value.code == 403


def test_http_origin_rebinding_and_no_arbitrary_command(http_monitor, monkeypatch):
    monitor, url = http_monitor
    with urllib.request.urlopen(url) as response:
        token = re.search(r"const token\s*=\s*[\"']([^\"']+)[\"']", response.read().decode()).group(1)
    for path, headers in [("/", {"Host": "other.example"}),
                          ("/api/start", {"X-Control-Token": token, "Origin": "https://other.example"})]:
        req = urllib.request.Request(url + path, method="GET" if path == "/" else "POST", headers=headers)
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(req)
        assert error.value.code == 403
    calls = []
    monkeypatch.setattr(monitor, "start", lambda: calls.append("configured worker") or {"message": "started"})
    req = urllib.request.Request(url + "/api/start", data=b'{"command":["untrusted"]}',
                                 headers={"X-Control-Token": token, "Origin": url}, method="POST")
    with urllib.request.urlopen(req) as response:
        assert response.status == 200
    assert calls == ["configured worker"]


def test_cli_parses_worker_argv_without_shell_interpretation():
    args = build_parser().parse_args(["monitor", "--total", "10", "--session-glob", "batches/*/progress.json",
                                     "--worker", "python", "script with spaces.py", "--flag", "a&b"])
    assert args.command == "monitor"
    assert args.worker == ["python", "script with spaces.py", "--flag", "a&b"]
    assert args.total == 10


def test_packaged_html_uses_text_nodes_for_untrusted_results():
    import re_agent.monitor.server as server

    html = Path(server.__file__).with_name("index.html").read_text(encoding="utf-8")
    assert "textContent" in html
    assert "innerHTML" not in html


def test_execution_status_reconciles_dead_process_and_preserves_verdicts(tmp_path):
    session = tmp_path / "session.json"
    save(session, {"1": {"address": "1", "success": False, "verdict": "PASS", "validation_verdict": "FAIL"}})
    status = session.with_suffix(".json.execution.json")
    status.write_text(json.dumps({"schema_version": 1, "phase": "running", "pid": 99999999, "created": 0,
                                 "jobs": [{"address": "2", "state": "running", "stage": "reverser"}]}))
    snapshot = Monitor(tmp_path, tmp_path / "state", ["session.json"]).snapshot()
    assert snapshot["executions"][0]["phase"] == "interrupted"
    assert snapshot["executions"][0]["jobs"][0]["stage"] == "interrupted"
    assert snapshot["recent"][0]["verdict"] == "PASS"
    assert snapshot["recent"][0]["validation_verdict"] == "FAIL"


def test_parallel_stop_requests_cleanup_then_force_stops(tmp_path):
    script = tmp_path / "worker.py"
    script.write_text(
        "import json, os, pathlib, psutil, time\n"
        "pathlib.Path('session.json').write_text('{}')\n"
        "pathlib.Path('session.json.execution.json').write_text(json.dumps({"
        "'schema_version': 1, 'phase': 'running', 'pid': os.getpid(), "
        "'created': psutil.Process().create_time(), 'jobs': []}))\n"
        "time.sleep(60)\n"
    )
    monitor = Monitor(tmp_path, tmp_path / "state", ["session.json"], worker=[sys.executable, str(script)])
    monitor.start()
    try:
        deadline = time.monotonic() + 10
        while not monitor.executions() and time.monotonic() < deadline:
            time.sleep(.02)
        assert monitor.executions()
        assert "Stop requested" in monitor.stop()["message"]
        assert (tmp_path / "session.json.execution.stop").exists()
        assert monitor.snapshot()["phase"] == "stopping"
        assert monitor.active()
        monitor.stop()
        assert not monitor.active()
    finally:
        if monitor.active():
            monitor.stop()
