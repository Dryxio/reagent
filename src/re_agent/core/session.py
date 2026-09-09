"""JSON-backed persistent session state for tracking reversal progress."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from pathlib import Path
from typing import Any

from re_agent.core.models import ReversalResult
from re_agent.utils.address import normalize_address
from re_agent.utils.storage import atomic_json, file_lock


class Session:
    """Tracks reversal progress in a JSON file."""

    def __init__(self, path: str | Path = "re-agent-progress.json") -> None:
        self.path = Path(path)
        self._data: dict[str, Any] = {"functions": {}, "runs": []}
        if self.path.exists():
            self.load()

    def load(self) -> None:
        data = json.loads(self.path.read_text(encoding="utf-8"))
        if (
            not isinstance(data, dict)
            or not isinstance(data.get("functions"), dict)
            or not isinstance(data.get("runs"), list)
        ):
            raise ValueError(f"Invalid session schema: {self.path}; preserve and repair this file")
        self._data = data

    def save(self) -> None:
        atomic_json(self.path, self._data)

    def bind(self, identity: str) -> None:
        """Archive old results when the project/evidence/acceptance policy changes."""
        with file_lock(self.path):
            if self.path.exists():
                self.load()
            previous = self._data.get("identity")
            if previous != identity and (self._data["functions"] or self._data.get("checkpoints")):
                history = self._data.get("history", [])
                history.append({k: v for k, v in self._data.items() if k != "history"})
                self._data = {"functions": {}, "runs": [], "history": history}
            self._data["identity"] = identity
            self.save()

    def record_checkpoint(self, result: ReversalResult) -> None:
        from re_agent.reports.formatter import _result_to_dict

        with file_lock(self.path):
            if self.path.exists():
                self.load()
            self._data.setdefault("checkpoints", {})[normalize_address(result.target.address)] = _result_to_dict(result)
            self.save()

    def previous_feedback(self, address: str) -> str:
        entry = self._data.get("checkpoints", {}).get(normalize_address(address))
        return json.dumps(entry, indent=2) if entry else ""

    def record_result(self, result: ReversalResult) -> None:
        addr = normalize_address(result.target.address)
        entry = {
            "address": result.target.address,
            "run_id": result.run_id,
            "error": result.error,
            "code_sha256": hashlib.sha256(result.code.encode()).hexdigest(),
            "code": result.code,
            "objective_verdict": result.objective_verdict.verdict.value if result.objective_verdict else None,
            "validation_checks": result.validation_verdict.checks if result.validation_verdict else [],
            "objective_findings": result.objective_verdict.findings if result.objective_verdict else [],
            "validation_findings": result.validation_verdict.findings if result.validation_verdict else [],
            "parity_findings": [asdict(f) for f in result.parity_findings],
            "class_name": result.target.class_name,
            "function_name": result.target.function_name,
            "success": result.success,
            "rounds_used": result.rounds_used,
            "verdict": result.checker_verdict.verdict.value if result.checker_verdict else None,
            "validation_verdict": (result.validation_verdict.verdict.value if result.validation_verdict else None),
            "parity_status": result.parity_status.value if result.parity_status else None,
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        with file_lock(self.path):
            if self.path.exists():
                self.load()
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
            1 for entry in self._data.get("runs", []) if normalize_address(str(entry.get("address", ""))) == addr
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


    @property
    def identity(self) -> str | None:
        """Fingerprint of current records; reading never rebinds or archives them."""
        value = self._data.get("identity")
        return value if isinstance(value, str) else None

    def get_checkpoint(self, address: str) -> dict[str, Any] | None:
        value = self._data.get("checkpoints", {}).get(normalize_address(address))
        return dict(value) if isinstance(value, dict) else None
