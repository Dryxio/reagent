"""Ranks and selects the next function to reverse in a class."""

from __future__ import annotations

from re_agent.backend.protocol import REBackend
from re_agent.core.models import FunctionTarget
from re_agent.core.session import Session
from re_agent.utils.address import normalize_address


def pick_next(
    class_name: str,
    backend: REBackend,
    session: Session,
    strategy: str = "high-impact",
    max_attempts_per_function: int = 3,
) -> FunctionTarget | None:
    """Pick the next function to reverse in a class.

    Filters out already-completed functions, ranks by caller_count (descending).
    Returns None if no candidates remain.
    """
    selection_error = None
    try:
        remaining = backend.remaining(class_name)
    except Exception as exc:
        selection_error = exc
        remaining = []

    if not remaining:
        try:
            remaining = backend.unimplemented(class_name)
        except Exception as exc:
            if selection_error is not None:
                raise RuntimeError(f"Cannot enumerate functions: {selection_error}; {exc}") from exc
            return None

    candidates = [
        f
        for f in remaining
        if not session.is_completed(f.address) and session.attempt_count(f.address) < max_attempts_per_function
    ]

    if not candidates:
        return None

    if strategy == "dependency-order":
        by_address = {normalize_address(f.address): f for f in candidates}
        ordered: list[str] = []
        visited: set[str] = set()
        active: set[str] = set()

        def visit(address: str) -> None:
            if address in visited or address in active:
                return
            active.add(address)
            try:
                dependencies = backend.xrefs_from(by_address[address].address)
            except Exception as exc:
                raise RuntimeError(f"Cannot read dependencies for {address}: {exc}") from exc
            for dependency in sorted({normalize_address(x.address) for x in dependencies}):
                if dependency in by_address:
                    visit(dependency)
            active.remove(address)
            visited.add(address)
            ordered.append(address)

        for candidate in sorted(candidates, key=lambda f: (f.caller_count, f.name, f.address)):
            visit(normalize_address(candidate.address))
        candidates = [by_address[address] for address in ordered]
    elif strategy == "easiest-first":
        candidates.sort(key=lambda f: (f.caller_count, f.name, f.address))
    else:
        candidates.sort(key=lambda f: (-f.caller_count, f.name, f.address))
    best = candidates[0]

    return FunctionTarget(
        address=best.address,
        class_name=best.class_name or class_name,
        function_name=best.name,
        caller_count=best.caller_count,
    )
