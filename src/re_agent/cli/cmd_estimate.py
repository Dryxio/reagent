"""Preflight token and optional API-cost estimates."""

from __future__ import annotations

import argparse
import math
from pathlib import Path

from re_agent.backend.registry import create_backend
from re_agent.config.loader import load_config


def cmd_estimate(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    reverser_config = config.agents.reverser or config.llm
    checker_config = config.agents.checker or config.llm
    backend = create_backend(config.backend)
    targets: list[str] = []
    if args.address:
        targets = [args.address]
    elif args.class_name:
        targets = [entry.address for entry in backend.remaining(args.class_name)[: args.limit]]
    else:
        print("Error: specify --address or --class")
        return 1

    base_input_tokens = 0
    inspected = 0
    for target in targets:
        try:
            raw = backend.decompile(target).raw_output
        except Exception:
            continue
        # Four characters/token plus a conservative allowance for source and evidence.
        base_input_tokens += math.ceil(len(raw) / 4) + 3000
        inspected += 1
    rounds = max(config.orchestrator.max_review_rounds, 1)
    investigations = config.orchestrator.max_investigations if config.orchestrator.investigation_enabled else 0
    reverser_calls = inspected * rounds * (1 + investigations)
    checker_calls = inspected * rounds
    # Include growing conversation/candidate context. This is deliberately a
    # planning bound, not a tokenizer-exact quote from a provider.
    input_tokens = base_input_tokens * (rounds * (1 + investigations) + rounds)
    input_tokens += inspected * rounds * reverser_config.max_tokens
    reverser_output = reverser_calls * reverser_config.max_tokens
    checker_output = checker_calls * checker_config.max_tokens
    output_tokens = reverser_output + checker_output
    input_cost = (
        base_input_tokens * rounds * (1 + investigations) / 1_000_000 * reverser_config.input_cost_per_million
        + (base_input_tokens * rounds + inspected * rounds * reverser_config.max_tokens)
        / 1_000_000
        * checker_config.input_cost_per_million
    )
    output_cost = (
        reverser_output / 1_000_000 * reverser_config.output_cost_per_million
        + checker_output / 1_000_000 * checker_config.output_cost_per_million
    )

    print(f"Functions inspected: {inspected}/{len(targets)}")
    print(f"Configured call bound: {reverser_calls} reverser + {checker_calls} checker")
    print(f"Planning input-token estimate: {input_tokens:,}")
    print(f"Planning output-token allowance: {output_tokens:,}")
    cli_roles = [
        role
        for role, provider in (
            ("reverser", reverser_config.provider),
            ("checker", checker_config.provider),
        )
        if provider in {"claude-cli", "codex"}
    ]
    if cli_roles:
        print("Note: max_tokens is an estimate, not a hard provider limit, for CLI roles: " + ", ".join(cli_roles))
    if input_cost or output_cost:
        print(f"Estimated API cost: ${input_cost + output_cost:.2f}")
    else:
        print("Estimated API cost: not configured (set per-million token prices)")
    return 0 if inspected else 1
