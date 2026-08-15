"""JSON-backed persistent session state for tracking reversal progress."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

from re_agent.core.models import ReversalResult
from re_agent.utils.address import normalize_address


class Session:
    """Tracks reversal progress in a JSON file."""

    def __init__(self, path: str | Path = "re-agent-progress.json") -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {"functions": {}, "runs": []}
        if self.path.exists():
            self.load()

    def load(self) -> None:
        try:
            self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            self._data = {"functions": {}, "runs": []}

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._data, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def record_result(self, result: ReversalResult) -> None:
        addr = normalize_address(result.target.address)
        entry = {
            "address": result.target.address,
            "class_name": result.target.class_name,
            "function_name": result.target.function_name,
            "success": result.success,
            "rounds_used": result.rounds_used,
            "verdict": result.checker_verdict.verdict.value if result.checker_verdict else None,
            "validation_verdict": (
                result.validation_verdict.verdict.value if result.validation_verdict else None
            ),
            "parity_status": result.parity_status.value if result.parity_status else None,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._data["functions"][addr] = entry
        self._data["runs"].append(entry)
        self.save()

    def is_completed(self, address: str) -> bool:
        addr = normalize_address(address)
        func = self._data["functions"].get(addr)
        return func is not None and func.get("success", False)

    def is_attempted(self, address: str) -> bool:
        """Return True if this address has been attempted (pass or fail)."""
        addr = normalize_address(address)
        return addr in self._data["functions"]

    def attempt_count(self, address: str) -> int:
        """Return the number of recorded runs for an address."""
        addr = normalize_address(address)
        return sum(
            1
            for entry in self._data.get("runs", [])
            if normalize_address(str(entry.get("address", ""))) == addr
        )

    def get_class_summary(self, class_name: str) -> dict[str, int]:
        total = 0
        passed = 0
        failed = 0
        for func in self._data["functions"].values():
            if func.get("class_name") == class_name:
                total += 1
                if func.get("success"):
                    passed += 1
                else:
                    failed += 1
        return {"total": total, "passed": passed, "failed": failed}

    def get_summary(self) -> dict[str, Any]:
        funcs = self._data["functions"]
        total = len(funcs)
        passed = sum(1 for f in funcs.values() if f.get("success"))
        failed = total - passed
        classes: set[str] = set()
        for f in funcs.values():
            cn = f.get("class_name", "")
            if cn:
                classes.add(cn)
        return {
            "total_functions": total,
            "passed": passed,
            "failed": failed,
            "classes_touched": len(classes),
        }

    def get_all_functions(self) -> list[dict[str, Any]]:
        return list(self._data["functions"].values())
