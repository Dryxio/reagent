"""Compare observable JSON results from a reference and a candidate harness.

Each harness consumes one JSON value on stdin and emits one JSON value on stdout.
Projects define their ABI adapter and include return values, writes and effects in
that value. This compares recorded observables; it is not a proof of equivalence.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from re_agent.utils.process import run_process


@dataclass
class DifferentialResult:
    passed: bool
    cases_run: int = 0
    findings: list[str] = field(default_factory=list)


def compare_commands(
    reference: list[str],
    candidate: list[str],
    cases: list[Any],
    cwd: Path,
    timeout_s: int = 30,
) -> DifferentialResult:
    if not reference or not candidate or not cases:
        return DifferentialResult(False, findings=["Differential validation requires two harnesses and nonempty cases"])
    for number, case in enumerate(cases, 1):
        outputs = []
        for role, command in (("reference", reference), ("candidate", candidate)):
            try:
                proc = run_process(
                    command, input_text=json.dumps(case, allow_nan=False), cwd=str(cwd), timeout_s=timeout_s
                )
                if proc.returncode:
                    return DifferentialResult(
                        False, number, [f"{role} harness exit {proc.returncode}: {proc.stderr[-4000:]}"]
                    )

                def reject_constant(value: str) -> None:
                    raise ValueError(f"Non-standard JSON constant: {value}")

                outputs.append(json.loads(proc.stdout, parse_constant=reject_constant))
            except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
                return DifferentialResult(False, number, [f"{role} harness failed: {exc}"])
        # Compare canonical JSON, preserving bool vs int and exact float encoding.
        if json.dumps(outputs[0], sort_keys=True) != json.dumps(outputs[1], sort_keys=True):
            return DifferentialResult(
                False,
                number,
                [
                    json.dumps(
                        {
                            "case": case,
                            "reference": outputs[0],
                            "candidate": outputs[1],
                        },
                        sort_keys=True,
                    )
                ],
            )
    return DifferentialResult(True, len(cases), [f"All {len(cases)} differential cases matched"])
