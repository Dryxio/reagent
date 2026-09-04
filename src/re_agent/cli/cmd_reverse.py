"""re-agent reverse command — single function or class reversal."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from re_agent.config.loader import load_config, validate_config
from re_agent.config.schema import ReAgentConfig
from re_agent.core.models import FunctionTarget
from re_agent.reports.formatter import format_result


def cmd_reverse(args: argparse.Namespace) -> int:
    if not args.address and not args.class_name:
        print("Error: specify --address or --class", file=sys.stderr)
        return 1
    config = load_config(Path(args.config))

    if args.max_rounds is not None:
        config.orchestrator.max_review_rounds = args.max_rounds
    if args.skip_parity:
        config.parity.enabled = False

    validate_config(config)

    if args.dry_run:
        return _dry_run(args, config)

    validation = config.validation
    if validation.enabled and validation.require_verified:
        commands = validation.build_commands + validation.test_commands + validation.runtime_commands
        if not (commands or validation.differential_cases_file) or not validation.trust_configured_commands:
            raise ValueError("Verified reversal requires configured trusted validation gates; run re-agent doctor")

    # Lazy imports to avoid loading LLM/backend unless needed
    from re_agent.backend.registry import create_backend
    from re_agent.core.session import Session
    from re_agent.llm.registry import create_provider

    reverser_llm = create_provider(config.agents.reverser or config.llm)
    checker_llm = create_provider(config.agents.checker or config.llm)
    backend = create_backend(config.backend)
    session = Session(config.output.session_file)
    from re_agent.core.identity import project_fingerprint

    session.bind(project_fingerprint(config))

    if args.address:
        from re_agent.orchestrator.single import reverse_single

        class_name = args.class_name or ""
        function_name = ""

        dec = backend.decompile(args.address)
        if dec.name:
            resolved_class, _, function_name = dec.name.rpartition("::")
            function_name = function_name or dec.name
            class_name = class_name or resolved_class
        # Project hooks provide identity when legacy decompilation uses FUN_* names.
        from re_agent.parity.source_indexer import SourceIndexer
        from re_agent.utils.address import normalize_address

        source_index = SourceIndexer(Path(config.project_profile.source_root), config.project_profile)
        for address, (hook_class, hook_name) in source_index.hook_address_index.items():
            if normalize_address(address) == normalize_address(args.address):
                class_name = hook_class or class_name
                function_name = hook_name
                break

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
            indexer=source_index,
        )
        from re_agent.reports.formatter import results_to_json, results_to_markdown

        if config.output.format == "json":
            print(results_to_json([result]))
        elif config.output.format == "markdown":
            print(results_to_markdown([result]))
        else:
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
        from re_agent.reports.formatter import results_to_json, results_to_markdown

        if config.output.format == "json":
            print(results_to_json(results))
        elif config.output.format == "markdown":
            print(results_to_markdown(results))
        else:
            for result in results:
                print(format_result(result))
        passed = sum(1 for r in results if r.success)
        total = len(results)
        print(f"Results: {passed}/{total} passed", file=sys.stderr)
        return 0 if passed == total else 1

    print("Error: specify --address or --class", file=sys.stderr)
    return 1


def _dry_run(args: argparse.Namespace, config: ReAgentConfig) -> int:
    print("Dry run mode — no LLM calls will be made.\n")

    if args.address:
        print(f"Would reverse: {args.address}")
        if args.class_name:
            print(f"  Class: {args.class_name}")
        return 0

    if args.class_name:
        print(f"Would reverse functions in class: {args.class_name}")
        from re_agent.backend.registry import create_backend

        backend = create_backend(config.backend)
        entries = backend.remaining(args.class_name)
        max_fn = args.max_functions or config.orchestrator.max_functions_per_class
        for entry in entries[:max_fn]:
            print(f"  {entry.address}  {entry.class_name}::{entry.name}")
        print(f"  Max functions: {max_fn}")
        print(f"  Max rounds per function: {config.orchestrator.max_review_rounds}")
        return 0

    print("Error: specify --address or --class", file=sys.stderr)
    return 1
