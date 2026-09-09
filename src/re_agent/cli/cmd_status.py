"""re-agent status command — show reversal progress."""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from re_agent.config.loader import load_config
from re_agent.core.session import Session
from re_agent.reports.tracker import ProgressTracker


def cmd_status(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    session = Session(config.output.session_file)
    if getattr(args, "manifest", None):
        if args.class_name:
            raise ValueError("--manifest cannot be combined with --class")
        from re_agent.core.identity import project_fingerprint
        from re_agent.core.target_plan import TargetPlan
        from re_agent.reports.coverage import format_coverage, manifest_coverage

        report = manifest_coverage(TargetPlan.load(Path(args.manifest)), session, project_fingerprint(config))
        print(json.dumps(report, indent=2) if args.format == "json"
              else format_coverage(report, markdown=args.format == "markdown"))
        return 0
    tracker = ProgressTracker(session)

    if args.format == "json":
        data = tracker.get_function_table(args.class_name) if args.class_name else session.get_summary()
        print(json.dumps(data, indent=2))
        return 0

    if args.format == "markdown":
        rows = tracker.get_function_table(args.class_name)
        if not rows:
            print("No functions recorded yet.")
            return 0
        print("| Address | Class | Function | Status | Rounds | Time |")
        print("|---------|-------|----------|--------|--------|------|")
        for r in rows:
            addr = r['address']
            cls = r['class']
            fn = r['function']
            st = r['status']
            rds = r['rounds']
            ts = r['timestamp']
            print(f"| {addr} | {cls} | {fn} | {st} | {rds} | {ts} |")
        return 0

    # Default: text format
    if args.class_name:
        print(tracker.print_class_summary(args.class_name))
        print()
        rows = tracker.get_function_table(args.class_name)
        for r in rows:
            print(f"  {r['address']}  {r['function']:40s}  {r['status']:4s}  ({r['rounds']} rounds)")
    else:
        print(tracker.print_summary())

    return 0
