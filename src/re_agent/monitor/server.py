"""Loopback-only progress reporting and explicitly configured worker control."""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import select
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from re_agent.monitor.events import AgentEvents
from re_agent.utils.storage import atomic_json, file_lock


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
        return value if isinstance(value, dict) else {}
    except (OSError, ValueError):
        return {}


class Monitor:
    """Read sessions without changing them; control only a configured child worker."""

    def __init__(self, work_dir: Path, state_dir: Path, session_globs: list[str],
                 log_glob: str | None = None, total: int = 0, worker: list[str] | None = None,
                 progress_file: str | None = None, stop_file: str | None = None,
                 event_glob: str | None = None) -> None:
        if total < 0:
            raise ValueError("Total function count must be nonnegative")
        for pattern in [*session_globs, *([log_glob] if log_glob else []), *([event_glob] if event_glob else [])]:
            if Path(pattern).anchor or ".." in Path(pattern).parts:
                raise ValueError("Monitor patterns must be relative to the working directory")
        self.work_dir = work_dir.resolve()
        for value in (progress_file, stop_file):
            if value and (Path(value).is_absolute()
                          or not (self.work_dir / value).resolve().is_relative_to(self.work_dir)):
                raise ValueError("Progress and stop files must be relative to the working directory")
        if stop_file and not progress_file:
            raise ValueError("A stop file requires a progress file")
        if progress_file and worker and not stop_file:
            raise ValueError("Managed batch progress requires a cooperative stop file")
        self.progress_file = self.work_dir / progress_file if progress_file else None
        self.stop_file = self.work_dir / stop_file if stop_file else None
        self.state_dir = state_dir.resolve()
        self.session_globs = session_globs
        self.log_glob = log_glob
        self.event_glob = event_glob
        self.agent_events = AgentEvents()
        self.total = total
        self.worker = worker or []
        self.record = self.state_dir / "worker.json"
        self.process: Any = None
        self.psutil: Any = None
        if self.worker:
            try:
                import psutil
            except ImportError as exc:
                raise RuntimeError("Worker controls require: pip install 'auto-re-agent[monitor]'") from exc
            self.psutil = psutil
        self.lock = threading.RLock()
        self.cache: dict[Path, tuple[tuple[int, int], list[dict[str, Any]]]] = {}
        self._adopt()

    def _adopt(self) -> None:
        if not self.worker:
            return
        saved = read_json(self.record)
        try:
            if saved.get("command") != self.worker or saved.get("cwd") != str(self.work_dir):
                return
            process = self.psutil.Process(saved["pid"])
            # Launchers may exec another interpreter and rewrite argv on startup.
            # A per-launch marker survives exec without relaxing PID reuse checks.
            marker = saved.get("launch_marker")
            identity_matches = (process.environ().get("RE_AGENT_MONITOR_WORKER") == marker if marker
                                else process.cmdline() == saved.get("process_command", self.worker))
            if process.create_time() == saved["created"] and identity_matches:
                self.process = process
        except (self.psutil.Error, KeyError):
            pass

    def active(self) -> bool:
        if self.process is None:
            return False
        try:
            if isinstance(self.process, self.psutil.Popen) and self.process.poll() is not None:
                return False
            return bool(self.process.is_running() and self.process.status() != self.psutil.STATUS_ZOMBIE)
        except self.psutil.Error:
            return False

    def start(self) -> dict[str, Any]:
        if not self.worker:
            raise ValueError("Read-only monitor: no worker command configured")
        with self.lock, file_lock(self.record):
            self._adopt()
            if self.active():
                return {"message": "Already running", "pid": self.process.pid}
            if self.progress_file:
                saved_progress = read_json(self.progress_file)
                pid = saved_progress.get("pid")
                if type(pid) is int and self.psutil.pid_exists(pid):
                    raise ValueError("A recorded external worker still exists; refusing a duplicate launch")
            if self.stop_file:
                if not self.stop_file.resolve().is_relative_to(self.work_dir):
                    raise ValueError("Stop file escaped the working directory")
                self.stop_file.unlink(missing_ok=True)
            self.state_dir.mkdir(parents=True, exist_ok=True)
            marker = secrets.token_urlsafe(32)
            with (self.state_dir / "worker.stdout.log").open("ab") as out, \
                    (self.state_dir / "worker.stderr.log").open("ab") as err:
                self.process = self.psutil.Popen(
                    self.worker, cwd=self.work_dir, stdout=out, stderr=err,
                    env={**os.environ, "RE_AGENT_MONITOR_WORKER": marker},
                    creationflags=int(getattr(subprocess, "CREATE_NO_WINDOW", 0)) if os.name == "nt" else 0,
                    start_new_session=os.name != "nt",
                )
            atomic_json(self.record, {"pid": self.process.pid, "created": self.process.create_time(),
                                      "command": self.worker, "launch_marker": marker,
                                      "cwd": str(self.work_dir), "phase": "running"})
            return {"message": "Worker started", "pid": self.process.pid}

    def executions(self) -> list[tuple[Path, dict[str, Any]]]:
        """Read adjacent status files, verifying PID birth time before displaying activity."""
        import psutil

        paths = {p for pattern in self.session_globs
                 for p in self.work_dir.glob(pattern + ".execution.json") if p.is_file()}
        result = []
        for path in sorted(paths):
            data = read_json(path)
            if data.get("schema_version") != 1:
                continue
            alive = False
            try:
                process = psutil.Process(data["pid"])
                alive = process.create_time() == data["created"] and process.status() != psutil.STATUS_ZOMBIE
            except (psutil.Error, KeyError, TypeError):
                pass
            if not alive and data.get("phase") in {"running", "stopping"}:
                data["phase"] = "interrupted"
                for job in data.get("jobs", []):
                    if job.get("state") in {"running", "proposed"}:
                        job.update(state="interrupted", stage="interrupted")
            data["alive"] = alive
            result.append((path, data))
        return result

    def stop(self) -> dict[str, str]:
        if self.stop_file is not None:
            if not self.stop_file.resolve().is_relative_to(self.work_dir):
                raise ValueError("Stop file escaped the working directory")
            self.stop_file.touch()
            return {"message": "Cooperative stop requested; the external runner handles cancellation."}
        if not self.worker:
            raise ValueError("Read-only monitor: no worker command configured")
        with self.lock, file_lock(self.record):
            self._adopt()
            if self.active():
                descendants = self.process.children(recursive=True)
                owned = {self.process.pid, *(child.pid for child in descendants)}
                executions = [(path, data) for path, data in self.executions()
                              if data.get("alive") and data.get("pid") in owned
                              and data.get("phase") in {"running", "stopping"}]
                saved = read_json(self.record)
                if executions and saved.get("phase") != "stopping":
                    for path, _data in executions:
                        path.with_suffix(".stop").touch()
                    saved.update(phase="stopping", stop_requested_at=time.time())
                    atomic_json(self.record, saved)
                    return {"message": "Stop requested; waiting for requests and cleanup. Stop again to force."}
                if os.name == "nt":
                    subprocess.run(["taskkill", "/PID", str(self.process.pid), "/T", "/F"],
                                   capture_output=True, timeout=20, check=True)
                else:
                    import signal

                    os.killpg(self.process.pid, signal.SIGTERM)
                    try:
                        self.process.wait(timeout=5)
                    except self.psutil.TimeoutExpired:
                        os.killpg(self.process.pid, signal.SIGKILL)
                    # Descendants can ignore SIGTERM even when the parent exits.
                    with contextlib.suppress(ProcessLookupError):
                        os.killpg(self.process.pid, signal.SIGKILL)
                for child in reversed(descendants):
                    with contextlib.suppress(self.psutil.Error):
                        child.kill()
                self.psutil.wait_procs(descendants, timeout=5)
                self.process.wait(timeout=20)
                saved = read_json(self.record)
                saved.update(phase="stopped", stopped_at=time.time())
                atomic_json(self.record, saved)
            return {"message": "Worker stopped; saved results retained"}

    def snapshot(self) -> dict[str, Any]:
        with self.lock:
            paths = {p for pattern in self.session_globs for p in self.work_dir.glob(pattern) if p.is_file()}
            for stale in self.cache.keys() - paths:
                del self.cache[stale]
            functions: dict[str, dict[str, Any]] = {}
            for path in sorted(paths):
                try:
                    stat = path.stat()
                    stamp = (stat.st_mtime_ns, stat.st_size)
                except OSError:
                    continue
                if path not in self.cache or self.cache[path][0] != stamp:
                    data = read_json(path).get("functions")
                    if isinstance(data, dict):
                        rows = [{key: row.get(key) for key in
                                 ("address", "function_name", "success", "rounds_used", "timestamp",
                                  "verdict", "validation_verdict")}
                                for row in data.values()
                                if isinstance(row, dict) and isinstance(row.get("address"), str)]
                        self.cache[path] = (stamp, rows)
                for row in self.cache.get(path, ((0, 0), []))[1]:
                    address = row["address"].lower().removeprefix("0x").lstrip("0") or "0"
                    prior = functions.get(address)
                    if prior is None or str(row.get("timestamp") or "") >= str(prior.get("timestamp") or ""):
                        functions[address] = row
            recent = sorted(functions.values(), key=lambda r: str(r.get("timestamp") or ""), reverse=True)
            passed = sum(row["success"] is True for row in recent)
            rounds = sum(row["rounds_used"] for row in recent if type(row.get("rounds_used")) is int)
            tail = ""
            try:
                logs = (list(self.work_dir.glob(self.log_glob)) if self.log_glob
                        else [self.state_dir / "worker.stderr.log"])
                log = max((p for p in logs if p.is_file()), key=lambda p: p.stat().st_mtime_ns, default=None)
                if log:
                    with log.open("rb") as stream:
                        stream.seek(max(0, log.stat().st_size - 7000))
                        tail = stream.read().decode("utf-8", errors="replace")
            except OSError:
                pass
            self._adopt()
            active = self.active()
            saved = read_json(self.record)
            matching = saved.get("command") == self.worker and saved.get("cwd") == str(self.work_dir)
            phase = "idle" if self.worker else "read-only"
            if active:
                phase = "stopping" if saved.get("phase") == "stopping" else "running"
            elif self.worker and matching:
                phase = "stopped" if saved.get("phase") in {"stopped", "stopping"} else "exited"
            snapshot = {"active": active, "controls": bool(self.worker),
                    "phase": phase, "executions": [data for _, data in self.executions()],
                    "targets": self.total, "completed": len(recent), "passed": passed,
                    "failed": len(recent) - passed, "rounds": rounds, "recent": recent[:18], "log": tail,
                    "updated": time.strftime("%H:%M:%S"), "output": str(self.work_dir)}
            if self.progress_file is not None:
                snapshot.update(self.external_progress())
            if self.event_glob:
                event_paths = [p for p in self.work_dir.glob(self.event_glob)
                         if p.is_file() and p.resolve().is_relative_to(self.work_dir)]
                latest = max(event_paths, key=lambda p: p.stat().st_mtime_ns, default=None)
                if latest:
                    snapshot["agents"] = self.agent_events.read(latest)
                snapshot["event_sources"] = [str(p.relative_to(self.work_dir)).replace("\\", "/")
                                             for p in sorted(event_paths)]
            return snapshot

    def external_progress(self) -> dict[str, Any]:
        """Adapt a cooperative batch runner's progress without adopting its process."""
        data = read_json(self.progress_file) if self.progress_file else {}
        def count(key: str) -> int:
            value = data.get(key, 0)
            return max(0, value) if type(value) is int else 0
        phase = str(data.get("phase", "waiting-for-progress"))
        updated = data.get("updated")
        fresh = isinstance(updated, (int, float)) and 0 <= time.time() - updated < 60
        running_phases = {"opening-analysis", "exporting-evidence", "native-subagents", "validating-candidates"}
        active = fresh and phase in running_phases
        if self.worker:
            active = self.active()
            if active and phase not in running_phases:
                phase = "starting"
        if active and self.stop_file and self.stop_file.exists():
            phase = "stopping"
        elif not fresh and phase in running_phases:
            phase = "status-stale"
        recent = []
        for row in data.get("recent", []) if isinstance(data.get("recent"), list) else []:
            if not isinstance(row, dict) or not isinstance(row.get("address"), str):
                continue
            recent.append({"address": row["address"], "success": False,
                           "result_label": "Compiled draft" if row.get("compiled") is True else "Needs attention",
                           "verdict": "Not reviewed",
                           "validation_verdict": "Build PASS" if row.get("compiled") is True else "FAIL",
                           "rounds_used": 0})
        summary = (f"Batch {count('batch')} / {count('batches')} · "
                   f"{count('child_started')} native children started · {count('child_returned')} results collected · "
                   f"{count('active_children')} awaiting collection")
        started = data.get("started")
        elapsed = max(0.0, time.time() - started) if isinstance(started, (int, float)) else 0.0
        batch_started = data.get("batch_started")
        batch_elapsed = max(0.0, time.time() - batch_started) if isinstance(batch_started, (int, float)) else 0.0
        rate = count("completed") * 60 / elapsed if elapsed else 0.0
        details = [
            {"label": "Elapsed", "value": f"{elapsed / 60:.1f} minutes"},
            {"label": "Throughput", "value": f"{rate:.2f} functions/minute"},
            {"label": "Current batch elapsed", "value": f"{batch_elapsed / 60:.1f} minutes"},
            {"label": "Remaining functions", "value": str(max(0, count("total") - count("completed")))},
            {"label": "Native children started", "value": str(count("child_started"))},
            {"label": "Results collected", "value": str(count("child_returned"))},
            {"label": "Awaiting collection", "value": str(count("active_children"))},
            {"label": "Behaviorally verified", "value": str(count("verified"))},
            {"label": "Progress freshness", "value": "Current" if fresh else "Stale or unavailable"},
        ]
        rows = data.get("recent")
        if not isinstance(rows, list):
            rows = []
        diagnostics = [f"{row['address']}: {row['diagnostic']}"
                       for row in rows if isinstance(row, dict)
                       and row.get("address") and row.get("diagnostic")]
        return {"active": bool(active), "controls": self.stop_file is not None, "can_start": bool(self.worker),
                "can_stop": self.stop_file is not None and phase != "stopping", "force_stop": False,
                "phase": phase, "targets": count("total"), "completed": count("completed"),
                "passed": count("compiled"), "failed": count("failed"), "rounds": 0,
                "passed_label": "Compiled drafts",
                "passed_detail": "Syntax checks passed; not accepted reconstructions",
                "result_note": ("Native candidates are unverified drafts. "
                                "Model review and behavioral acceptance have not run."),
                "recent": recent[-18:][::-1], "executions": [], "progress_summary": summary,
                "error": data.get("error"), "activity": summary, "details": details,
                "data_age_s": max(0, time.time() - updated) if isinstance(updated, (int, float)) else None,
                "diagnostics": "\n".join(diagnostics)[-7000:]}


def make_server(monitor: Monitor, port: int = 8765) -> ThreadingHTTPServer:
    """Bind only loopback; never accept worker commands from HTTP clients."""
    token = secrets.token_urlsafe(32)

    class Handler(BaseHTTPRequestHandler):
        server: ThreadingHTTPServer

        def reply(self, code: int, body: str, content_type: str = "application/json") -> None:
            payload = body.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", content_type + "; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Content-Security-Policy", "frame-ancestors 'none'")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def valid_host(self) -> bool:
            return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

        def do_GET(self) -> None:
            if not self.valid_host():
                self.reply(403, '{"error":"Invalid host"}')
            elif self.path == "/":
                html = Path(__file__).with_name("index.html").read_text(encoding="utf-8")
                self.reply(200, html.replace("__TOKEN__", token), "text/html")
            elif self.path == "/api/status":
                self.reply(200, json.dumps(monitor.snapshot()))
            elif self.path == "/api/stream":
                self.stream()
            elif urlsplit(self.path).path == "/api/agent-history":
                source = parse_qs(urlsplit(self.path).query).get("source", [""])[0]
                allowed = {str(p.relative_to(monitor.work_dir)).replace("\\", "/"): p
                           for p in monitor.work_dir.glob(monitor.event_glob or "__no_events__")
                           if p.is_file() and p.resolve().is_relative_to(monitor.work_dir)}
                if source not in allowed:
                    self.reply(404, '{"error":"Unknown event source"}')
                    return
                reader = AgentEvents()
                path = allowed[source]
                # Bound historical reads; the live stream remains incremental.
                for _ in range(32):
                    agents = reader.read(path)
                    if reader.offset >= path.stat().st_size:
                        break
                self.reply(200, json.dumps({"agents": agents, "truncated": reader.offset < path.stat().st_size}))
            else:
                self.reply(404, '{"error":"Not found"}')

        def stream(self) -> None:
            from wsproto import ConnectionType, WSConnection
            from wsproto.events import AcceptConnection, CloseConnection, Ping, TextMessage
            from wsproto.utilities import ProtocolError

            origin = f"http://127.0.0.1:{self.server.server_port}"
            if self.headers.get("Origin") != origin or self.headers.get("Upgrade", "").lower() != "websocket":
                self.reply(403, '{"error":"Stream request rejected"}')
                return
            ws = WSConnection(ConnectionType.SERVER)
            self.close_connection = True
            try:
                ws.initiate_upgrade_connection(
                    [(k.encode(), v.encode()) for k, v in self.headers.items()], "/api/stream")
                list(ws.events())
                self.connection.settimeout(5)
                self.connection.sendall(ws.send(AcceptConnection()))
                previous = ""
                first = True
                while True:
                    data = monitor.snapshot()
                    encoded = json.dumps(data)
                    if encoded != previous:
                        self.connection.sendall(ws.send(TextMessage(data=json.dumps({
                            "type": "snapshot" if first else "update", "data": data}))))
                        previous, first = encoded, False
                    ready, _, _ = select.select([self.connection], [], [], 0.5)
                    if ready:
                        payload = self.connection.recv(4096)
                        if not payload:
                            return
                        ws.receive_data(payload)
                        for event in ws.events():
                            if isinstance(event, CloseConnection):
                                self.connection.sendall(ws.send(event.response()))
                                return
                            if isinstance(event, Ping):
                                self.connection.sendall(ws.send(event.response()))
                            if isinstance(event, TextMessage):
                                self.connection.sendall(ws.send(CloseConnection(code=1008, reason="Read-only stream")))
                                return
            except (OSError, ProtocolError, ValueError):
                return

        def do_POST(self) -> None:
            origin = f"http://127.0.0.1:{self.server.server_port}"
            if (not self.valid_host() or self.headers.get("Origin") not in (None, origin)
                    or self.headers.get("X-Control-Token") != token):
                self.reply(403, '{"error":"Control request rejected"}')
                return
            try:
                if self.path == "/api/start":
                    result = monitor.start()
                elif self.path == "/api/stop":
                    result = monitor.stop()
                else:
                    self.reply(404, '{"error":"Not found"}')
                    return
                self.reply(200, json.dumps(result))
            except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                self.reply(400, json.dumps({"error": str(exc)}))

        def log_message(self, format: str, *args: Any) -> None:
            pass

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)
