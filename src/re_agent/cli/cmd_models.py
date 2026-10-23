"""List OrcaRouter Cyber Inference models from the live catalog."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from re_agent.config.loader import load_config
from re_agent.llm.orcarouter import (
    ORCAROUTER_DISPLAY_NAME,
    ORCAROUTER_PROVIDER,
    OrcaRouterError,
    discover_models,
)


def cmd_models(args: argparse.Namespace) -> int:
    """Discover the catalog and print models for one entry point."""
    config = load_config(Path(args.config))
    orca_config = next(
        (
            item
            for item in (config.agents.reverser, config.agents.checker, config.llm)
            if item is not None and item.provider == ORCAROUTER_PROVIDER
        ),
        None,
    )
    api_key = orca_config.api_key if orca_config is not None else None
    base_url = args.base_url or (orca_config.base_url if orca_config is not None else None)

    try:
        catalog = discover_models(
            api_key=api_key,
            base_url=base_url,
            capability=args.capability,
            timeout_s=min(config.llm.timeout_s, 30),
        )
    except (OrcaRouterError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(catalog.to_dict(), indent=2))
    else:
        print(
            f"{ORCAROUTER_DISPLAY_NAME}: {len(catalog.models)} model(s) "
            f"for capability={args.capability} (source={catalog.source})"
        )
        for model in catalog.models:
            context = str(model.context_length) if model.context_length is not None else "unknown"
            endpoints = ",".join(model.supported_endpoint_types) or "unknown"
            modalities = ",".join(model.input_modalities) or "unknown"
            print(f"{model.id}\tcontext={context}\tendpoints={endpoints}\tinput={modalities}")
        if not catalog.models:
            print("No models matched this capability.", file=sys.stderr)

    if catalog.degraded:
        print(
            f"Warning: live catalog unavailable; showing the verified fallback seed. {catalog.error}",
            file=sys.stderr,
        )
    return 0
