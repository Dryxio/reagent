"""Thread-local execution controls shared by providers and validation gates."""
from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any


class Cancelled(RuntimeError):
    pass


@dataclass
class Execution:
    cancel: threading.Event
    validations: threading.Semaphore
    emit: Callable[[str, Any], None]

    def fail(self, category: str, message: str) -> None:
        self.emit("fatal", {"category": category, "message": message})
        self.cancel.set()

    def check(self) -> None:
        if self.cancel.is_set():
            raise Cancelled("Execution cancelled")


_local = threading.local()


def current() -> Execution | None:
    return getattr(_local, "execution", None)


@contextmanager
def executing(context: Execution) -> Iterator[None]:
    previous = current()
    _local.execution = context
    try:
        context.check()
        yield
    finally:
        _local.execution = previous


def progress(stage: str) -> None:
    context = current()
    if context:
        context.check()
        context.emit("stage", stage)


@contextmanager
def validation_lane() -> Iterator[None]:
    context = current()
    if context is None:
        yield
        return
    progress("waiting-for-validation")
    while not context.validations.acquire(timeout=.1):
        context.check()
    try:
        progress("validating")
        yield
        context.check()
    finally:
        context.validations.release()


@contextmanager
def cancellation_signals(cancel: threading.Event) -> Iterator[None]:
    """Turn interrupt/termination into orderly shutdown on CLI main threads."""
    import signal

    if threading.current_thread() is not threading.main_thread():
        yield
        return
    previous = {sig: signal.getsignal(sig) for sig in (signal.SIGINT, signal.SIGTERM)}
    try:
        for sig in previous:
            signal.signal(sig, lambda signum, frame: cancel.set())
        yield
    finally:
        for sig, handler in previous.items():
            signal.signal(sig, handler)
