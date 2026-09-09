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
