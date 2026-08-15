"""re-agent reverse command — single function or class reversal."""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from re_agent.config.loader import load_config
from re_agent.core.models import FunctionTarget
from re_agent.reports.formatter import format_result


def cmd_reverse(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))

    if args.max_rounds is not None:
        config.orchestrator.max_review_rounds = args.max_rounds
    if args.skip_parity:
        config.parity.enabled = False

    if args.dry_run:
        return _dry_run(args, config)

    # Lazy imports to avoid loading LLM/backend unless needed
    from re_agent.backend.registry import create_backend
    from re_agent.core.session import Session
    from re_agent.llm.registry import create_provider

    reverser_llm = create_provider(config.agents.reverser or config.llm)
    checker_llm = create_provider(config.agents.checker or config.llm)
    backend = create_backend(config.backend)
    session = Session(config.output.session_file)

    if args.address:
        from re_agent.orchestrator.single import reverse_single

        class_name = args.class_name or ""
        function_name = args.function_name or ""

        # Try to resolve function metadata from the backend
        if not class_name or not function_name:
            try:
                dec = backend.decompile(args.address)
                if dec.name and "::" in dec.name:
                    detected_class, _, detected_function = dec.name.rpartition("::")
                    if not class_name:
                        class_name = detected_class
                    if not function_name:
                        function_name = detected_function
                elif dec.name and not function_name:
                    function_name = dec.name
            except Exception:
                pass  # Best-effort; proceed with empty metadata

        target = FunctionTarget(
            address=args.address,
            class_name=class_name,
            function_name=function_name,
        )
        result = reverse_single(
            target,
            config,
            backend,
            reverser_llm,
            checker_llm=checker_llm,
            session=session,
        )
        print(format_result(result))
        return 0 if result.success else 1

    if args.class_name:
        from re_agent.orchestrator.class_runner import reverse_class

        results = reverse_class(
            class_name=args.class_name,
            config=config,
            backend=backend,
            llm=reverser_llm,
            checker_llm=checker_llm,
            session=session,
            max_functions=args.max_functions,
        )
        for r in results:
            print(format_result(r))
            print()

        passed = sum(1 for r in results if r.success)
        total = len(results)
        print(f"\nResults: {passed}/{total} passed")
        return 0 if passed == total else 1

    print("Error: specify --address or --class", file=sys.stderr)
    return 1


def _dry_run(args: argparse.Namespace, config: object) -> int:
    print("Dry run mode — no LLM calls will be made.\n")

    if args.address:
        print(f"Would reverse: {args.address}")
        if args.class_name:
            print(f"  Class: {args.class_name}")
        if args.function_name:
            print(f"  Function: {args.function_name}")
        return 0

    if args.class_name:
        print(f"Would reverse functions in class: {args.class_name}")
        max_fn = args.max_functions or 10
        print(f"  Max functions: {max_fn}")
        print(f"  Max rounds per function: {args.max_rounds or 4}")
        return 0

    print("Error: specify --address or --class", file=sys.stderr)
    return 1
