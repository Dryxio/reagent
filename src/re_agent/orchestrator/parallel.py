"""Bounded function workers with coordinator-owned journals and checkpoints."""
from __future__ import annotations

import copy
import hashlib
import json
import os
import queue
import threading
import time
import uuid
from collections.abc import Callable
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import nullcontext
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, cast

from re_agent.backend.protocol import REBackend
from re_agent.config.loader import validate_config
from re_agent.config.schema import LLMConfig, ReAgentConfig
from re_agent.core.identity import project_fingerprint
from re_agent.core.models import (
    CheckerVerdict,
    Finding,
    FunctionTarget,
    ObjectiveVerdict,
    ParityStatus,
    ReversalResult,
    ValidationVerdict,
    Verdict,
)
from re_agent.core.session import Session
from re_agent.llm.protocol import LLMProvider
from re_agent.orchestrator.dependencies import prerequisites
from re_agent.orchestrator.execution import Cancelled, Execution, executing
from re_agent.orchestrator.single import reverse_single
from re_agent.reports.formatter import _result_to_dict
from re_agent.utils.address import normalize_address
from re_agent.utils.storage import atomic_json

ProviderFactory = Callable[[LLMConfig], LLMProvider]


def decode_result(data: dict[str, Any]) -> ReversalResult:
    result = ReversalResult(FunctionTarget(data["address"], data["class_name"], data["function_name"]),
                            code=data.get("code", ""), success=data.get("success", False),
                            rounds_used=data.get("rounds_used", 0), run_id=data.get("run_id", ""),
                            error=data.get("error"))
    if data.get("verdict"):
        result.checker_verdict = CheckerVerdict(Verdict(data["verdict"]), data.get("summary", ""),
                                                data.get("issues", []))
    if data.get("objective_verdict"):
        result.objective_verdict = ObjectiveVerdict(Verdict(data["objective_verdict"]),
            data.get("objective_summary", ""), data.get("objective_findings", []))
    if data.get("validation_verdict"):
        result.validation_verdict = ValidationVerdict(Verdict(data["validation_verdict"]),
            data.get("validation_summary", ""), data.get("validation_findings", []),
            overlay_file=data.get("candidate_overlay"), checks=data.get("validation_checks", []))
    if data.get("parity_status"):
        result.parity_status = ParityStatus(data["parity_status"])
    result.parity_findings = [Finding(**f) for f in data.get("parity_findings", [])]
    return result


class LockedBackend:
    """Serialize stateful backend calls; do not share provider or indexer state."""

    def __init__(self, backend: REBackend, lock: threading.Lock, cancel: threading.Event):
        self.backend, self.lock, self.cancel = backend, lock, cancel

    def __getattr__(self, name: str) -> Any:
        value = getattr(self.backend, name)
        if not callable(value):
            return value

        def invoke(*args: Any, **kwargs: Any) -> Any:
            while not self.lock.acquire(timeout=.1):
                if self.cancel.is_set():
                    raise Cancelled("Backend wait cancelled")
            try:
                if self.cancel.is_set():
                    raise Cancelled("Backend call cancelled")
                return copy.deepcopy(value(*args, **kwargs))
            except OSError as exc:
                from re_agent.orchestrator.execution import current

                context = current()
                if context:
                    context.fail("evidence", str(exc))
                raise
            finally:
                self.lock.release()
        return invoke


class WorkerSession(Session):
    def __init__(self, feedback: str, emit: Callable[[str, Any], None], offset: int):
        self.feedback, self.emit, self.offset = feedback, emit, offset

    def previous_feedback(self, address: str) -> str:
        return self.feedback

    def record_checkpoint(self, result: ReversalResult) -> None:
        saved = copy.deepcopy(result)
        saved.rounds_used += self.offset
        self.emit("checkpoint", saved)

    def record_result(self, result: ReversalResult, *, idempotent: bool = False) -> None:
        pass  # Only the coordinator publishes final results.


@dataclass
class Event:
    job: str
    kind: str
    value: Any
    ack: threading.Event


def reverse_parallel(
    targets: list[FunctionTarget], config: ReAgentConfig, backend: REBackend, session: Session,
    provider_factory: ProviderFactory, limit: int, *, cancel: threading.Event | None = None,
    promote: Callable[[ReversalResult, REBackend], ReversalResult] | None = None,
    identity: str | None = None,
) -> list[ReversalResult]:
    """Execute jobs; promotion, when supplied, is serialized by planned target order."""
    validate_config(config)
    cancel = cancel or threading.Event()
    if limit < 1:
        raise ValueError("Function attempt limit must be positive")
    policy = asdict(config.orchestrator)
    for key in ("max_parallel_functions", "max_parallel_validations", "max_functions_per_class"):
        policy.pop(key, None)
    models = [asdict(c) for c in (config.agents.reverser or config.llm, config.agents.checker or config.llm)]
    for model in models:
        model.pop("api_key", None)
    identity = identity or project_fingerprint(config)
    key = hashlib.sha256(json.dumps([identity, str(session.path.resolve()), policy, models],
                                   sort_keys=True).encode()).hexdigest()[:24]
    root = Path(config.output.report_dir).resolve() / "parallel" / key
    root.mkdir(parents=True, exist_ok=True)
    from re_agent.orchestrator.execution import cancellation_signals

    with session.coordinator(), cancellation_signals(cancel):
        return _run(targets, config, backend, session, provider_factory, limit, cancel, root, promote)


def _run(targets: list[FunctionTarget], config: ReAgentConfig, backend: REBackend, session: Session,
         factory: ProviderFactory, limit: int, cancel: threading.Event, root: Path,
         promote: Callable[[ReversalResult, REBackend], ReversalResult] | None) -> list[ReversalResult]:
    by_address = {normalize_address(t.address): t for t in targets}
    order = list(by_address)
    rank = {a: i for i, a in enumerate(order)}
    deps = prerequisites(order, {
        a: {normalize_address(x.address) for x in backend.xrefs_from(t.address)}
        for a, t in by_address.items()
    }) if config.orchestrator.selection_strategy == "dependency-order" else {a: set() for a in order}
    journal = root / "jobs"
    journal.mkdir(exist_ok=True)
    jobs: dict[str, Any] = {p.stem: json.loads(p.read_text(encoding="utf-8")) for p in sorted(journal.glob("*.json"))}
    jobs = dict(sorted(jobs.items(), key=lambda pair: pair[1]["sequence"]))
    attempts = session.attempt_counts()
    completed = {normalize_address(v["address"]) for v in session.get_all_functions() if v.get("success")}
    status = session.path.with_suffix(session.path.suffix + ".execution.json")
    stop = status.with_suffix(".stop")
    stop.unlink(missing_ok=True)
    events: queue.Queue[Event] = queue.Queue()
    gate = threading.Semaphore(config.orchestrator.max_parallel_validations)
    backend_lock = threading.Lock()
    generation_lock = threading.Lock()
    futures: dict[Future[ReversalResult], str] = {}
    proposed: dict[str, ReversalResult] = {}
    results: list[ReversalResult] = []
    submitted = 0
    failure: dict[str, str] | None = None
    watcher_done = threading.Event()

    def watch_stop() -> None:
        while not watcher_done.wait(.1):
            if stop.exists():
                cancel.set()

    watcher = threading.Thread(target=watch_stop, daemon=True)

    import psutil

    created = psutil.Process().create_time()

    def save(changed: str | None = None) -> None:
        if changed is not None:
            atomic_json(journal / f"{changed}.json", jobs[changed])
        active = {v["address"] for v in jobs.values() if v["state"] in {"running", "proposed"}}
        visible = [v for v in jobs.values() if v["state"] in {"running", "proposed"}]
        visible += [v for v in jobs.values() if v["state"] not in {"running", "proposed"}][-32:]
        atomic_json(status, {"schema_version": 1, "phase": "stopping" if cancel.is_set() else "running",
            "pid": os.getpid(), "created": created, "run_id": root.name,
            "limit": config.orchestrator.max_parallel_functions,
            "queued": sum(a not in completed and a not in active and
                          attempts.get(a, 0) < config.orchestrator.max_attempts_per_function for a in order),
            "jobs": [{k: v for k, v in j.items() if k not in {"checkpoint", "result"}} for j in visible],
            "counts": {state: sum(v["state"] == state for v in jobs.values())
                       for state in ("running", "proposed", "completed", "failed", "interrupted")},
            "updated": time.time(), "error": failure, "stop_file": str(stop.resolve())})

    def publish(job: str, result: ReversalResult) -> None:
        # Journal first, then idempotent session publication: recovery can finish
        # either half of this transaction after a crash.
        jobs[job].update(state="completed" if result.success else "failed",
                         result=_result_to_dict(result), finished=time.time())
        save(job)
        session.record_result_once(result)
        address = normalize_address(result.target.address)
        attempts[address] = attempts.get(address, 0) + 1
        if result.success:
            completed.add(address)
        results.append(result)

    for value in jobs.values():
        if value["state"] in {"completed", "failed"}:
            recovered = decode_result(value["result"])
            if promote and recovered.success and not session.is_completed(recovered.target.address):
                recovered = promote(recovered, backend)
            session.record_result_once(recovered)
        else:
            value["state"] = "interrupted"
            save(value["id"])
    attempts = session.attempt_counts()
    completed = {normalize_address(v["address"]) for v in session.get_all_functions() if v.get("success")}

    def emit(job: str, kind: str, value: Any) -> None:
        event = Event(job, kind, value, threading.Event())
        events.put(event)
        if kind in {"stage", "fatal"}:
            return
        while not event.ack.wait(.1):
            if cancel.is_set():
                raise Cancelled("Checkpoint publication cancelled")

    def worker(job: str, snapshot: dict[str, Any]) -> ReversalResult:
        t = by_address[snapshot["address"]]
        isolated = copy.deepcopy(config)
        directory = root / job
        directory.mkdir(exist_ok=True)
        isolated.output.report_dir = str(directory)
        isolated.output.log_dir = str(directory / "logs")
        isolated.parity.cache_dir = str(directory / "parity-cache")
        isolated.orchestrator.max_parallel_functions = 1
        offset = int(snapshot.get("rounds", 0))
        isolated.orchestrator.max_review_rounds -= offset
        isolated.orchestrator.max_llm_calls_per_function -= int(snapshot.get("calls", 0))
        if min(isolated.orchestrator.max_review_rounds, isolated.orchestrator.max_llm_calls_per_function) <= 0:
            return ReversalResult(t, code="", success=False,
                                  error="Interrupted attempt exhausted its saved budget", run_id=job)
        providers: list[LLMProvider] = []
        try:
            from re_agent.orchestrator.snapshot import project_snapshot

            with (executing(Execution(cancel, gate, lambda kind, value: emit(job, kind, value))),
                  project_snapshot(isolated, generation_lock) if promote else nullcontext(isolated) as isolated):
                try:
                    providers = [factory(isolated.agents.reverser or isolated.llm)]
                    providers.append(factory(isolated.agents.checker or isolated.llm))
                except (ValueError, OSError) as exc:
                    emit(job, "fatal", {"category": "configuration", "message": str(exc)})
                    cancel.set()
                    raise
                feedback = json.dumps(snapshot.get("checkpoint", {}))
                result = reverse_single(t, isolated, cast(REBackend, LockedBackend(backend, backend_lock, cancel)),
                    providers[0], checker_llm=providers[1], session=WorkerSession(
                        feedback, lambda kind, value: emit(job, kind, value), offset))
                result.rounds_used += offset
                result.run_id = job
                return result
        finally:
            for provider in providers:
                close = getattr(provider, "close", None)
                if callable(close):
                    close()

    def drain() -> None:
        nonlocal failure
        while True:
            try:
                event = events.get_nowait()
            except queue.Empty:
                return
            try:
                if event.kind == "fatal":
                    failure = event.value
                    cancel.set()
                    continue
                if cancel.is_set():
                    continue
                value = jobs[event.job]
                if event.kind == "checkpoint":
                    value.update(checkpoint=_result_to_dict(event.value), rounds=event.value.rounds_used)
                    event.value.run_id = event.job
                    session.record_checkpoint(event.value)
                elif event.kind == "call":
                    value["calls"] = value.get("calls", 0) + 1
                else:
                    value["stage"] = event.value
                save(event.job)
            finally:
                event.ack.set()

    watcher.start()
    try:
        with ThreadPoolExecutor(max_workers=config.orchestrator.max_parallel_functions) as executor:
            try:
                while True:
                    if stop.exists():
                        cancel.set()
                    drain()
                    for future in list(futures):
                        if not future.done():
                            continue
                        job = futures.pop(future)
                        if cancel.is_set():
                            jobs[job]["state"] = "interrupted"
                            continue
                        try:
                            result = future.result()
                        except Exception as exc:
                            result = ReversalResult(by_address[jobs[job]["address"]], code="", success=False,
                                                    error=str(exc), run_id=job)
                        proposed[job] = result
                        jobs[job]["state"] = "proposed"
                    # Cumulative promotion follows dispatch order, never completion order.
                    for job in sorted(list(proposed), key=lambda j: jobs[j]["sequence"]):
                        if promote and any(jobs[j]["sequence"] < jobs[job]["sequence"] for j in futures.values()):
                            break
                        result = proposed.pop(job)
                        if cancel.is_set():
                            jobs[job]["state"] = "interrupted"
                            continue
                        if promote and result.success:
                            with generation_lock, executing(Execution(cancel, gate, lambda k, v: None)):
                                result = promote(result, cast(REBackend, LockedBackend(backend, backend_lock, cancel)))
                        if cancel.is_set():
                            jobs[job]["state"] = "interrupted"
                        else:
                            publish(job, result)
                    active_addresses = {jobs[j]["address"] for j in [*futures.values(), *proposed]}
                    pending = {a for a in order if a not in completed
                               and attempts.get(a, 0) < config.orchestrator.max_attempts_per_function}
                    terminal = set(order) - pending
                    while (not cancel.is_set() and
                           len(futures) + len(proposed) < config.orchestrator.max_parallel_functions and
                           submitted < limit):
                        ready = next((a for a in order if a in pending and a not in active_addresses
                                      and deps[a] <= terminal), None)
                        if ready is None:
                            break
                        job = next((j for j, v in jobs.items()
                                    if v["address"] == ready and v["state"] == "interrupted"), uuid.uuid4().hex)
                        value = jobs.setdefault(job, {"id": job, "address": ready, "calls": 0, "rounds": 0,
                                                      "sequence": len(jobs), "started": time.time(),
                                                      "checkpoint": session.get_checkpoint(ready) or {}})
                        value["state"] = "running"
                        save(job)
                        futures[executor.submit(worker, job, copy.deepcopy(value))] = job
                        active_addresses.add(ready)
                        submitted += 1
                    if not futures and not proposed:
                        break
                    time.sleep(.05)
            finally:
                # Ensure worker acknowledgement waits cannot deadlock shutdown.
                if futures:
                    cancel.set()
    except BaseException:
        cancel.set()
        raise
    finally:
        drain()
        watcher_done.set()
        watcher.join()
        for value in jobs.values():
            if value["state"] in {"running", "proposed"}:
                value["state"] = "interrupted"
            if value["state"] == "interrupted":
                save(value["id"])
        save()
        data = json.loads(status.read_text())
        data["phase"] = "failed" if failure else "stopped" if cancel.is_set() else "complete"
        atomic_json(status, data)
    return sorted(results, key=lambda r: (rank[normalize_address(r.target.address)], jobs[r.run_id]["sequence"]))
