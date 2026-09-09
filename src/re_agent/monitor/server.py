"""Loopback-only progress reporting and explicitly configured worker control."""
from __future__ import annotations

import contextlib
import json
import os
import secrets
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

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
                 log_glob: str | None = None, total: int = 0, worker: list[str] | None = None) -> None:
        if total < 0:
            raise ValueError("Total function count must be nonnegative")
        for pattern in [*session_globs, *([log_glob] if log_glob else [])]:
            if Path(pattern).anchor or ".." in Path(pattern).parts:
                raise ValueError("Monitor patterns must be relative to the working directory")
        self.work_dir = work_dir.resolve()
        self.state_dir = state_dir.resolve()
        self.session_globs = session_globs
        self.log_glob = log_glob
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
        self.cache: dict[Path, tuple[int, list[dict[str, Any]]]] = {}
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

    def stop(self) -> dict[str, str]:
        if not self.worker:
            raise ValueError("Read-only monitor: no worker command configured")
        with self.lock, file_lock(self.record):
            self._adopt()
            if self.active():
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
                    stamp = path.stat().st_mtime_ns
                except OSError:
                    continue
                if path not in self.cache or self.cache[path][0] != stamp:
                    data = read_json(path).get("functions")
                    if isinstance(data, dict):
                        rows = [{key: row.get(key) for key in
                                 ("address", "function_name", "success", "rounds_used", "timestamp")}
                                for row in data.values()
                                if isinstance(row, dict) and isinstance(row.get("address"), str)]
                        self.cache[path] = (stamp, rows)
                for row in self.cache.get(path, (0, []))[1]:
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
                phase = "running"
            elif self.worker and matching:
                phase = "stopped" if saved.get("phase") == "stopped" else "exited"
            return {"active": active, "controls": bool(self.worker),
                    "phase": phase,
                    "targets": self.total, "completed": len(recent), "passed": passed,
                    "failed": len(recent) - passed, "rounds": rounds, "recent": recent[:18], "log": tail,
                    "updated": time.strftime("%H:%M:%S"), "output": str(self.work_dir)}


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
            else:
                self.reply(404, '{"error":"Not found"}')

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
