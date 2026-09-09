"""Create a bounded target manifest without loading an LLM provider."""
from __future__ import annotations

import argparse
from pathlib import Path

from re_agent.backend.registry import create_backend
from re_agent.config.loader import load_config
from re_agent.core.identity import project_fingerprint
from re_agent.core.target_plan import build_plan


def cmd_plan(args: argparse.Namespace) -> int:
    config = load_config(Path(args.config))
    backend = create_backend(config.backend)
    seeds = list(args.address or [])
    for pattern in args.match or []:
        if not backend.capabilities.has_search:
            raise ValueError("Backend does not support symbol search")
        matches = backend.search(pattern)
        if not matches:
            raise ValueError(f"No functions matched {pattern!r}")
        seeds.extend(entry.address for entry in matches)
    plan = build_plan(backend, seeds, project_fingerprint(config),
                      max_depth=args.max_depth, max_functions=args.max_functions)
    plan.save(Path(args.output))
    print(f"Planned {len(plan.functions)} functions; {len(plan.gaps)} evidence gaps. Manifest: {args.output}")
    return 0
