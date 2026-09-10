"""Configuration loader for re-agent."""

from __future__ import annotations

import dataclasses
import logging
import os
from pathlib import Path
from typing import Any, TypeVar

from re_agent.config.schema import (
    AgentModelsConfig,
    BackendConfig,
    LLMConfig,
    OrchestratorConfig,
    OutputConfig,
    ParityConfig,
    ProjectProfile,
    ReAgentConfig,
    ValidationConfig,
)


def _deep_merge(base: dict[str, Any], overlay: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge overlay into base, returning a new dict."""
    result = dict(base)
    for key, value in overlay.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def _load_yaml_file(path: Path) -> dict[str, Any]:
    """Load a YAML file and return its contents as a dict."""
    try:
        import yaml  # type: ignore[import-untyped]
    except ImportError as err:
        raise ImportError(
            "PyYAML is required for loading YAML config files. Install it with: pip install pyyaml"
        ) from err
    text = path.read_text(encoding="utf-8")
    data = yaml.safe_load(text)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Expected a YAML mapping at top level in {path}, got {type(data).__name__}")
    return data


def _apply_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Overlay RE_AGENT_* environment variables onto the raw config dict."""
    env_mappings: list[tuple[str, list[str], type]] = [
        ("RE_AGENT_LLM_PROVIDER", ["llm", "provider"], str),
        ("RE_AGENT_LLM_API_KEY", ["llm", "api_key"], str),
        ("RE_AGENT_LLM_MODEL", ["llm", "model"], str),
        ("RE_AGENT_LLM_BASE_URL", ["llm", "base_url"], str),
        ("RE_AGENT_BACKEND_CLI_PATH", ["backend", "cli_path"], str),
        ("RE_AGENT_BACKEND_TIMEOUT", ["backend", "timeout_s"], int),
    ]

    for env_var, key_path, cast_type in env_mappings:
        value = os.environ.get(env_var)
        if value is None:
            continue

        # Navigate to the correct nested dict, creating intermediates as needed.
        d = raw
        for part in key_path[:-1]:
            if part not in d or not isinstance(d[part], dict):
                d[part] = {}
            d = d[part]
        d[key_path[-1]] = cast_type(value)

    return raw


def _apply_cli_overrides(raw: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Apply CLI overrides using dot-notation keys (e.g., 'llm.model')."""
    for dotted_key, value in overrides.items():
        parts = dotted_key.split(".")
        d = raw
        for part in parts[:-1]:
            if part not in d or not isinstance(d[part], dict):
                d[part] = {}
            d = d[part]
        d[parts[-1]] = value
    return raw


def _coerce_field(value: Any, field_type_str: str) -> Any:
    """Best-effort coercion of a value to match a dataclass field type string."""
    if value is None:
        return value
    # Handle stringified type annotations (from __future__ import annotations)
    if "int" in field_type_str and not isinstance(value, int):
        try:
            return int(value)
        except (ValueError, TypeError):
            return value
    if "float" in field_type_str and not isinstance(value, (int, float)):
        try:
            return float(value)
        except (ValueError, TypeError):
            return value
    if "bool" in field_type_str and not isinstance(value, bool):
        if isinstance(value, str):
            return value.lower() in ("true", "1", "yes")
        return bool(value)
    return value


_T = TypeVar("_T")


_log = logging.getLogger(__name__)


def _build_with_coercion(cls: type[_T], data: dict[str, Any]) -> _T:
    """Build a dataclass from a raw dict, coercing types and warning on unknowns."""
    known = {f.name: f for f in dataclasses.fields(cls)}  # type: ignore[arg-type]
    filtered: dict[str, Any] = {}
    for k, v in data.items():
        if k in known:
            ft = known[k].type
            type_str = ft if isinstance(ft, str) else getattr(ft, "__name__", str(ft))
            filtered[k] = _coerce_field(v, type_str)
        else:
            _log.warning(
                "Unknown config key '%s' in %s (known: %s) — ignored",
                k,
                cls.__name__,
                ", ".join(sorted(known)),
            )
    return cls(**filtered)


def _build_project_profile(data: dict[str, Any]) -> ProjectProfile:
    """Build a ProjectProfile from a raw dict, ignoring unknown keys."""
    return _build_with_coercion(ProjectProfile, data)


def _build_llm_config(data: dict[str, Any]) -> LLMConfig:
    return _build_with_coercion(LLMConfig, data)


def _build_backend_config(data: dict[str, Any]) -> BackendConfig:
    return _build_with_coercion(BackendConfig, data)


def _build_agents_config(data: dict[str, Any]) -> AgentModelsConfig:
    def role(name: str) -> LLMConfig | None:
        value = data.get(name)
        if value is None:
            return None
        if not isinstance(value, dict):
            raise ValueError(f"agents.{name} must be a mapping")
        return _build_llm_config(value)

    return AgentModelsConfig(reverser=role("reverser"), checker=role("checker"))


def _build_parity_config(data: dict[str, Any]) -> ParityConfig:
    return _build_with_coercion(ParityConfig, data)


def _build_orchestrator_config(data: dict[str, Any]) -> OrchestratorConfig:
    return _build_with_coercion(OrchestratorConfig, data)


def _build_output_config(data: dict[str, Any]) -> OutputConfig:
    return _build_with_coercion(OutputConfig, data)


def _build_validation_config(data: dict[str, Any]) -> ValidationConfig:
    return _build_with_coercion(ValidationConfig, data)


def _build_config(raw: dict[str, Any]) -> ReAgentConfig:
    """Build a ReAgentConfig from a raw dict."""
    return ReAgentConfig(
        project_profile=_build_project_profile(raw.get("project_profile", {})),
        llm=_build_llm_config(raw.get("llm", {})),
        agents=_build_agents_config(raw.get("agents", {})),
        backend=_build_backend_config(raw.get("backend", {})),
        parity=_build_parity_config(raw.get("parity", {})),
        orchestrator=_build_orchestrator_config(raw.get("orchestrator", {})),
        validation=_build_validation_config(raw.get("validation", {})),
        output=_build_output_config(raw.get("output", {})),
    )


def load_config(
    yaml_path: Path | None = None,
    cli_overrides: dict[str, Any] | None = None,
) -> ReAgentConfig:
    """Load configuration from YAML, environment variables, and CLI overrides.

    Priority (highest to lowest):
        1. CLI overrides (dot-notation keys, e.g., ``llm.model``)
        2. Environment variables (``RE_AGENT_*``)
        3. YAML file values
        4. Dataclass defaults

    Args:
        yaml_path: Path to the YAML configuration file.  If ``None``, the
            loader attempts ``re-agent.yaml`` in the current directory; if that
            does not exist, pure defaults are used.
        cli_overrides: Optional dict of dot-notation key/value overrides from
            the command line.

    Returns:
        A fully-populated :class:`ReAgentConfig` instance.
    """
    raw: dict[str, Any] = {}

    # 1. Load YAML file if available.
    if yaml_path is not None:
        if yaml_path.exists():
            raw = _load_yaml_file(yaml_path)
        else:
            raise FileNotFoundError(f"Config file not found: {yaml_path}")
    else:
        default_path = Path("re-agent.yaml")
        if default_path.exists():
            raw = _load_yaml_file(default_path)

    # 2. Overlay environment variables.
    raw = _apply_env_overrides(raw)

    # 3. Overlay CLI overrides.
    if cli_overrides:
        raw = _apply_cli_overrides(raw, cli_overrides)

    # 4. Build typed config from the merged dict.
    config = _build_config(raw)
    validate_config(config)
    return config


def validate_config(config: ReAgentConfig) -> None:
    workers = config.orchestrator.max_parallel_functions
    requests = config.orchestrator.max_parallel_requests
    retries = config.orchestrator.max_request_retries
    if type(requests) is not int or not 1 <= requests <= 32:
        raise ValueError("max_parallel_requests must be an integer from 1 to 32")
    if type(retries) is not int or not 0 <= retries <= 3:
        raise ValueError("max_request_retries must be an integer from 0 to 3")
    validations = config.orchestrator.max_parallel_validations
    if type(workers) is not int or not 1 <= workers <= 32:
        raise ValueError("max_parallel_functions must be an integer from 1 to 32")
    if type(validations) is not int or not 1 <= validations <= workers:
        raise ValueError("max_parallel_validations must be between 1 and max_parallel_functions")
    if validations > 1 and not (config.validation.parallel_safe and config.validation.copy_project):
        raise ValueError("Parallel validation requires copy_project and an explicit parallel_safe command contract")
    for name in (
        "max_review_rounds",
        "max_functions_per_class",
        "max_attempts_per_function",
        "max_llm_calls_per_function",
    ):
        value = getattr(config.orchestrator, name)
        if type(value) is not int or value < 1:
            raise ValueError(f"orchestrator.{name} must be a positive integer")
    if type(config.orchestrator.max_investigations) is not int or config.orchestrator.max_investigations < 0:
        raise ValueError("max_investigations must be a nonnegative integer")
    if config.orchestrator.selection_strategy not in {"dependency-order", "easiest-first", "high-impact"}:
        raise ValueError("Unknown selection_strategy")
    for name in (
        "build_commands",
        "test_commands",
        "runtime_commands",
        ):
        value = getattr(config.validation, name)
        if not isinstance(value, list) or not all(
            (isinstance(item, str) and bool(item.strip()))
            or (isinstance(item, list) and bool(item) and all(isinstance(arg, str) for arg in item)
                and bool(item[0].strip()))
            for item in value
        ):
            raise ValueError(f"validation.{name} must contain nonempty shell strings or argument arrays")
    for name in ("differential_reference", "differential_candidate"):
        value = getattr(config.validation, name)
        if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
            raise ValueError(f"validation.{name} must be a list of strings")
    if config.validation.command_timeout_s <= 0:
        raise ValueError("validation.command_timeout_s must be positive")
