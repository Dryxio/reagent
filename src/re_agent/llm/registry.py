"""LLM provider factory registry."""

from __future__ import annotations

from re_agent.config.schema import LLMConfig
from re_agent.llm.protocol import LLMProvider


def create_provider(config: LLMConfig) -> LLMProvider:
    """Instantiate an LLM provider from a configuration object.

    Args:
        config: The LLM configuration specifying provider type, model,
            API key, and other parameters.

    Returns:
        An object satisfying the :class:`LLMProvider` protocol.

    Raises:
        ValueError: If ``config.provider`` is not a recognised provider name.
    """
    if config.provider == "claude":
        from re_agent.llm.claude import ClaudeProvider

        return ClaudeProvider(
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout_s=config.timeout_s,
        )

    if config.provider == "claude-cli":
        from re_agent.llm.claude_cli import ClaudeCLIProvider

        return ClaudeCLIProvider(
            model=config.model or "sonnet",
            timeout_s=config.timeout_s,
            claude_bin=config.cli_path or "claude",
            max_budget_usd=config.max_budget_usd,
            effort=config.effort,
        )

    if config.provider in ("openai", "openai-compat"):
        from re_agent.llm.openai_compat import OpenAIProvider

        return OpenAIProvider(
            api_key=config.api_key,
            model=config.model,
            max_tokens=config.max_tokens,
            temperature=config.temperature,
            timeout_s=config.timeout_s,
            base_url=config.base_url,
        )

    if config.provider == "codex":
        from re_agent.llm.codex_cli import CodexCLIProvider

        return CodexCLIProvider(
            model=config.model or "gpt-5.4",
            codex_bin=config.cli_path or "codex",
            timeout_s=config.timeout_s,
        )

    raise ValueError(
        f"Unknown LLM provider: {config.provider!r}. "
        f"Supported providers: 'claude', 'claude-cli', 'openai', "
        f"'openai-compat', 'codex'."
    )
