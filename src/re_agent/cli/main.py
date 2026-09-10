"""CLI entry point for re-agent."""

from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="re-agent",
        description="Autonomous reverse engineering agent",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.4.0")
    parser.add_argument("--config", default="re-agent.yaml", help="Config file path")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    monitor_p = sub.add_parser("monitor", help="Host a local live progress dashboard")
    monitor_p.add_argument("--work-dir", default=".", help="Directory containing run sessions")
    monitor_p.add_argument("--state-dir", default="reports/monitor", help="Monitor control state and worker logs")
    monitor_p.add_argument("--session-glob", action="append", help="Relative session pattern (repeatable)")
    monitor_p.add_argument("--log-glob", help="Relative activity-log pattern; shows most recently modified log")
    monitor_p.add_argument("--total", type=int, default=0, help="Planned function count; 0 means unknown")
    monitor_p.add_argument("--port", type=int, default=8765)
    monitor_p.add_argument("--worker", nargs=argparse.REMAINDER, help="Optional worker argv; must be the last option")

    doctor_p = sub.add_parser("doctor", help="Check configuration and exported evidence without LLM calls")
    doctor_p.add_argument("--address", help="Check evidence for one function")
    benchmark_p = sub.add_parser("benchmark", help="Run a differential harness manifest")
    benchmark_p.add_argument("--manifest", required=True)
    benchmark_p.add_argument("--output")

    plan_p = sub.add_parser("plan", help="Export a bounded target manifest without LLM calls")
    plan_p.add_argument("--address", action="append", help="Seed function address (repeatable)")
    plan_p.add_argument("--match", action="append", help="Backend symbol search (repeatable)")
    plan_p.add_argument("--max-depth", type=int, default=1)
    plan_p.add_argument("--max-functions", type=int, default=100)
    plan_p.add_argument("--output", required=True)

    evidence_p = sub.add_parser("evidence", help="Export stored manifest evidence into searchable packets")
    evidence_p.add_argument("--manifest", required=True)
    evidence_p.add_argument("--output", required=True)

    # init
    init_p = sub.add_parser("init", help="Initialize re-agent.yaml config file")
    init_p.add_argument("--profile", default=None, help="Use a built-in project profile template")

    # reverse
    rev_p = sub.add_parser("reverse", help="Reverse engineer functions")
    rev_p.add_argument("--manifest", help="Target manifest produced by plan")
    rev_p.add_argument("--address", help="Single function address to reverse")
    rev_p.add_argument("--class", dest="class_name", help="Class name for class-level reversal")
    rev_p.add_argument("--max-functions", type=int, default=None, help="Max functions per class")
    rev_p.add_argument("--max-parallel-functions", type=int, default=None, help="Concurrent functions (1-32)")
    rev_p.add_argument("--max-parallel-requests", type=int, default=None, help="Concurrent model requests (1-32)")
    rev_p.add_argument("--max-parallel-validations", type=int, default=None, help="Concurrent isolated validations")
    rev_p.add_argument("--max-rounds", type=int, default=None, help="Max review rounds per function")
    rev_p.add_argument("--dry-run", action="store_true", help="Show plan without executing")
    rev_p.add_argument("--skip-parity", action="store_true", help="Skip parity check after PASS")

    # parity
    par_p = sub.add_parser("parity", help="Run parity checks on hooked functions")
    par_p.add_argument("--address", action="append", help="Specific address (repeatable)")
    par_p.add_argument("--filter", help="Regex filter on symbol/class")
    par_p.add_argument("--limit", type=int, help="Max functions to check")
    par_p.add_argument("--skip-ghidra", action="store_true", help="Source-only checks")
    par_p.add_argument("--strict-exit", action="store_true", help="Exit 1 on RED")
    par_p.add_argument("--output", help="Output JSON report path")

    # status
    stat_p = sub.add_parser("status", help="Show reversal progress")
    stat_p.add_argument("--manifest", help="Report coverage of a planned function group")
    stat_p.add_argument("--class", dest="class_name", help="Filter by class")
    stat_p.add_argument("--format", choices=["text", "json", "markdown"], default="text")

    # estimate
    estimate_p = sub.add_parser("estimate", help="Estimate token usage before a run")
    estimate_p.add_argument("--address", help="Single function address")
    estimate_p.add_argument("--class", dest="class_name", help="Class name to estimate")
    estimate_p.add_argument("--limit", type=int, default=50, help="Maximum functions to inspect")

    return parser


def _main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    if args.command == "monitor":
        from re_agent.cli.cmd_monitor import cmd_monitor

        return cmd_monitor(args)

    if args.command == "evidence":
        from re_agent.cli.cmd_evidence import cmd_evidence

        return cmd_evidence(args)
    if args.command == "plan":
        from re_agent.cli.cmd_plan import cmd_plan

        return cmd_plan(args)
    if args.command == "doctor":
        from re_agent.cli.cmd_doctor import cmd_doctor

        return cmd_doctor(args)
    if args.command == "benchmark":
        from re_agent.cli.cmd_benchmark import cmd_benchmark

        return cmd_benchmark(args)

    if args.command == "init":
        from re_agent.cli.cmd_init import cmd_init

        return cmd_init(args)

    if args.command == "reverse":
        from re_agent.cli.cmd_reverse import cmd_reverse

        return cmd_reverse(args)

    if args.command == "parity":
        from re_agent.cli.cmd_parity import cmd_parity

        return cmd_parity(args)

    if args.command == "status":
        from re_agent.cli.cmd_status import cmd_status

        return cmd_status(args)

    if args.command == "estimate":
        from re_agent.cli.cmd_estimate import cmd_estimate

        return cmd_estimate(args)

    parser.print_help()
    return 1


def main(argv: list[str] | None = None) -> int:
    try:
        return _main(argv)
    except (ValueError, RuntimeError, OSError) as exc:
        import sys

        print(f"Error: {exc}", file=sys.stderr)
        return 1
