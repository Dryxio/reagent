"""Export an existing target manifest's stored evidence."""
from __future__ import annotations

import argparse
from pathlib import Path

from re_agent.core.target_plan import TargetPlan
from re_agent.reports.evidence import export_evidence


def cmd_evidence(args: argparse.Namespace) -> int:
    plan = TargetPlan.load(Path(args.manifest))
    export_evidence(plan, Path(args.output))
    print(f"Exported {len(plan.functions)} function packets to {args.output}")
    return 0
