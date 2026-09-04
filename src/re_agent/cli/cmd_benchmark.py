"""Reproducible differential benchmark manifests."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from re_agent.utils.storage import atomic_json
from re_agent.verification.differential import compare_commands


def cmd_benchmark(args: argparse.Namespace) -> int:
    manifest = Path(args.manifest).resolve()
    entries = json.loads(manifest.read_text(encoding="utf-8"))
    if not isinstance(entries, list) or not entries:
        raise ValueError("Benchmark manifest must be a nonempty JSON array")
    results = []
    for entry in entries:
        start = time.monotonic()
        comparison = compare_commands(
            entry["reference"], entry["candidate"], entry["cases"], manifest.parent, int(entry.get("timeout_s", 30))
        )
        results.append(
            {
                "name": entry["name"],
                "matched": comparison.passed,
                "expected_match": entry.get("expected_match", True),
                "cases_run": comparison.cases_run,
                "findings": comparison.findings,
                "duration_s": time.monotonic() - start,
            }
        )
    report = {
        "schema_version": 1,
        "results": results,
        "total": len(results),
        "expectations_met": sum(r["matched"] == r["expected_match"] for r in results),
    }
    if args.output:
        atomic_json(Path(args.output), report)
    print(json.dumps(report, indent=2))
    return 0 if report["expectations_met"] == report["total"] else 1
