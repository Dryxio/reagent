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
    manifest_path = getattr(args, "manifest", None)
    if manifest_path and (args.address or args.class_name):
        raise ValueError("--manifest cannot be combined with --address or --class")
    if args.max_functions is not None and args.max_functions < 1:
        raise ValueError("--max-functions must be positive")
    if not args.address and not args.class_name and not manifest_path:
        print("Error: specify --address, --class, or --manifest", file=sys.stderr)
        return 1
    config = load_config(Path(args.config))

    if args.max_rounds is not None:
        config.orchestrator.max_review_rounds = args.max_rounds
    if args.skip_parity:
        config.parity.enabled = False

    for name in ("max_parallel_functions", "max_parallel_validations"):
        value = getattr(args, name, None)
        if value is not None:
            setattr(config.orchestrator, name, value)
    validate_config(config)

    from re_agent.core.identity import project_fingerprint
    from re_agent.core.target_plan import TargetPlan

    plan = TargetPlan.load(Path(manifest_path)) if manifest_path else None
    if plan is not None and plan.identity != project_fingerprint(config):
        raise ValueError("Target manifest inputs changed; regenerate with plan before reversal")
    if args.dry_run:
        if plan is not None:
            import json
            from dataclasses import asdict

            print(json.dumps({"functions": [asdict(target) for target in plan.functions],
                              "gaps": [asdict(gap) for gap in plan.gaps]}, indent=2))
            return 0
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

    # Parallel jobs construct their own providers; no unused shared clients.
    reverser_llm = checker_llm = None
    backend = create_backend(config.backend)
    session = Session(config.output.session_file)
    from contextlib import ExitStack

    with session.coordinator(), ExitStack() as resources:
        if args.address or config.orchestrator.max_parallel_functions == 1:
            providers = []
            for role in (config.agents.reverser or config.llm, config.agents.checker or config.llm):
                provider = create_provider(role)
                providers.append(provider)
                close = getattr(provider, "close", None)
                if callable(close):
                    resources.callback(close)
            reverser_llm, checker_llm = providers
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
            assert reverser_llm is not None
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

        if args.class_name or plan is not None:
            from re_agent.orchestrator.class_runner import reverse_class

            if plan is not None:
                from re_agent.orchestrator.batch_runner import reverse_manifest

                results = reverse_manifest(plan, config, backend, reverser_llm, session,
                                           args.max_functions, checker_llm, provider_factory=create_provider)
            else:
                results = reverse_class(
                    class_name=args.class_name,
                    config=config,
                    backend=backend,
                    llm=reverser_llm,
                    checker_llm=checker_llm,
                    session=session,
                    max_functions=args.max_functions,
                    provider_factory=create_provider,
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
            import json

            status_path = session.path.with_suffix(session.path.suffix + ".execution.json")
            if config.orchestrator.max_parallel_functions > 1 and status_path.exists():
                phase = json.loads(status_path.read_text(encoding="utf-8")).get("phase")
                if phase == "stopped":
                    return 130
                if phase == "failed":
                    return 1
            return 0 if passed == total else 1

        print("Error: specify --address, --class, or --manifest", file=sys.stderr)
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

    print("Error: specify --address, --class, or --manifest", file=sys.stderr)
    return 1
