"""CLI entry point for re-agent."""
from __future__ import annotations

import argparse


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="re-agent",
        description="Autonomous reverse engineering agent",
    )
    parser.add_argument("--version", action="version", version="%(prog)s 0.2.1")
    parser.add_argument("--config", default="re-agent.yaml", help="Config file path")

    sub = parser.add_subparsers(dest="command", help="Available commands")

    # init
    init_p = sub.add_parser("init", help="Initialize re-agent.yaml config file")
    init_p.add_argument("--profile", default=None, help="Use a built-in project profile template")

    # reverse
    rev_p = sub.add_parser("reverse", help="Reverse engineer functions")
    rev_p.add_argument("--address", help="Single function address to reverse")
    rev_p.add_argument("--class", dest="class_name", help="Class name for class-level reversal")
    rev_p.add_argument("--function", dest="function_name", help="Function name for single-address reversal")
    rev_p.add_argument("--max-functions", type=int, default=None, help="Max functions per class")
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
    stat_p.add_argument("--class", dest="class_name", help="Filter by class")
    stat_p.add_argument("--format", choices=["text", "json", "markdown"], default="text")

    # estimate
    estimate_p = sub.add_parser("estimate", help="Estimate token usage before a run")
    estimate_p.add_argument("--address", help="Single function address")
    estimate_p.add_argument("--class", dest="class_name", help="Class name to estimate")
    estimate_p.add_argument("--limit", type=int, default=50, help="Maximum functions to inspect")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

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
