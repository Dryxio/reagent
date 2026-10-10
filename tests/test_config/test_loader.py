"""Tests for config loading."""
from __future__ import annotations

from pathlib import Path

import pytest

from re_agent.config.loader import load_config, validate_config
from re_agent.config.schema import ReAgentConfig


def test_load_default_config() -> None:
    config = load_config(None)
    assert isinstance(config, ReAgentConfig)
    assert config.llm.provider == "claude"
    assert config.backend.type == "ghidra-bridge"
    assert config.backend.cli_path == "ghidra-bridge"
    assert config.orchestrator.max_review_rounds == 4
    assert config.orchestrator.objective_verifier_enabled is True


def test_load_from_yaml(sample_config_path: Path) -> None:
    config = load_config(sample_config_path)
    assert config.project_profile.stub_call_prefix == "plugin::Call"
    assert config.llm.model == "claude-sonnet-4-5-20250929"
    assert config.parity.call_count_warn_diff == 3


def test_role_specific_agent_configs(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(
        """\
llm:
  provider: claude
agents:
  reverser:
    provider: claude-cli
    model: sonnet
    max_budget_usd: 1.5
  checker:
    provider: codex
    model: gpt-5.4
""",
        encoding="utf-8",
    )
    config = load_config(path)
    assert config.agents.reverser is not None
    assert config.agents.reverser.provider == "claude-cli"
    assert config.agents.reverser.max_budget_usd == 1.5
    assert config.agents.checker is not None
    assert config.agents.checker.provider == "codex"


def test_cli_overrides() -> None:
    config = load_config(None, cli_overrides={"llm.provider": "openai", "orchestrator.max_review_rounds": "6"})
    assert config.llm.provider == "openai"
    assert config.orchestrator.max_review_rounds == 6


def test_env_override(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("RE_AGENT_LLM_PROVIDER", "openai")
    monkeypatch.setenv("RE_AGENT_LLM_MODEL", "gpt-4o")
    config = load_config(None)
    assert config.llm.provider == "openai"
    assert config.llm.model == "gpt-4o"


@pytest.mark.parametrize(
    ("attribute", "value", "message"),
    [
        ("max_review_rounds", 0, "orchestrator.max_review_rounds must be a positive integer"),
        ("max_attempts_per_function", True, "orchestrator.max_attempts_per_function must be a positive integer"),
        ("max_investigations", -1, "max_investigations must be a nonnegative integer"),
    ],
)
def test_validate_config_rejects_orchestrator_boundaries(
    attribute: str, value: object, message: str
) -> None:
    config = ReAgentConfig.create_default()
    setattr(config.orchestrator, attribute, value)

    with pytest.raises(ValueError, match=message):
        validate_config(config)


@pytest.mark.parametrize(
    ("attribute", "value"),
    [
        ("build_commands", ["   "]),
        ("test_commands", [["   ", "--quiet"]]),
        ("runtime_commands", [42]),
    ],
)
def test_validate_config_rejects_invalid_command_entries(attribute: str, value: object) -> None:
    config = ReAgentConfig.create_default()
    setattr(config.validation, attribute, value)

    with pytest.raises(
        ValueError, match=rf"validation.{attribute} must contain nonempty shell strings or argument arrays"
    ):
        validate_config(config)


def test_validate_config_accepts_command_boundaries() -> None:
    config = ReAgentConfig.create_default()
    config.validation.build_commands = [" make ", ["python", "-m", "pytest"]]
    config.validation.test_commands = []
    config.validation.runtime_commands = [["app"]]
    config.orchestrator.max_review_rounds = 1
    config.orchestrator.max_investigations = 0

    validate_config(config)
