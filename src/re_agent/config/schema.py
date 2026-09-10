"""Configuration schema dataclasses for re-agent."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class ProjectProfile:
    """Project-specific patterns and paths."""

    hook_patterns: list[str] = field(
        default_factory=lambda: [
            r"RH_ScopedInstall\s*\(\s*(\w+)\s*,\s*(0x[0-9A-Fa-f]+)",
            r"RH_ScopedVirtualInstall\s*\(\s*(\w+)\s*,\s*(0x[0-9A-Fa-f]+)",
        ]
    )
    stub_patterns: list[str] = field(
        default_factory=lambda: [
            r"plugin::Call",
        ]
    )
    stub_markers: list[str] = field(
        default_factory=lambda: [
            "NOTSA_UNREACHABLE",
        ]
    )
    stub_call_prefix: str = "plugin::Call"
    class_macro: str = "RH_ScopedClass"
    source_root: str = "source/game_sa"
    compilation_database: str | None = None
    source_extensions: list[str] = field(
        default_factory=lambda: [
            ".cpp",
            ".h",
            ".hpp",
        ]
    )
    hooks_csv: str | None = "docs/hooks.csv"
    name: str = "gta-reversed"
    language_standard: str = "C++23"
    prompt_rules: list[str] = field(
        default_factory=lambda: [
            "Use real member names from the existing project and reference headers",
            "Never call virtual methods on this inside hook implementations",
            "Use matrix.TransformVector(vec) instead of deprecated Multiply3x3",
            "Verify struct offsets against project VALIDATE_OFFSET checks",
        ]
    )


@dataclass
class LLMConfig:
    """LLM provider configuration."""

    provider: str = "claude"
    model: str = "claude-sonnet-4-5-20250929"
    api_key: str | None = None
    base_url: str | None = None
    max_tokens: int = 4096
    temperature: float = 0.0
    timeout_s: int = 1800
    cli_path: str | None = None
    max_budget_usd: float | None = None
    effort: str | None = None
    input_cost_per_million: float = 0.0
    output_cost_per_million: float = 0.0


@dataclass
class AgentModelsConfig:
    """Optional per-role model overrides.

    ``None`` keeps backwards compatibility by falling back to the top-level
    :class:`LLMConfig`.
    """

    reverser: LLMConfig | None = None
    checker: LLMConfig | None = None


@dataclass
class BackendConfig:
    """Decompiler backend configuration."""

    type: str = "ghidra-bridge"
    export_dir: str | None = None
    address_map: str | None = None
    cli_path: str = "ghidra-bridge"
    timeout_s: int = 45


@dataclass
class ParityConfig:
    """Static parity verification settings."""

    enabled: bool = True
    call_count_warn_diff: int = 3
    inline_wrapper_autoskip: bool = False
    semantic_rules_file: str | None = None
    manual_checks_file: str | None = None
    cache_dir: str = ".cache/re-agent-parity"


@dataclass
class OrchestratorConfig:
    """Orchestrator loop settings."""

    max_review_rounds: int = 4
    max_llm_calls_per_function: int = 80
    cumulative_validation: bool = True
    max_functions_per_class: int = 10
    objective_verifier_enabled: bool = True
    objective_call_count_tolerance: int = 3
    objective_control_flow_tolerance: int = 2
    investigation_enabled: bool = True
    max_investigations: int = 8
    selection_strategy: str = "dependency-order"
    max_attempts_per_function: int = 3
    max_parallel_functions: int = 1
    max_parallel_validations: int = 1
    max_parallel_requests: int = 1
    max_request_retries: int = 0


@dataclass
class ValidationConfig:
    """Candidate overlay, build, test, and acceptance gate settings."""

    enabled: bool = True
    copy_project: bool = False
    project_root: str = "."
    build_commands: list[str | list[str]] = field(default_factory=list)
    test_commands: list[str | list[str]] = field(default_factory=list)
    runtime_commands: list[str | list[str]] = field(default_factory=list)
    differential_reference: list[str] = field(default_factory=list)
    differential_candidate: list[str] = field(default_factory=list)
    differential_cases_file: str | None = None
    require_build: bool = False
    require_tests: bool = False
    require_runtime: bool = False
    require_verified: bool = True
    trust_configured_commands: bool = False
    parity_fail_on_red: bool = True
    parity_fail_on_yellow: bool = False
    command_timeout_s: int = 900
    working_directory: str = "."
    keep_project_copy: bool = False
    parallel_safe: bool = False


@dataclass
class OutputConfig:
    """Output and reporting settings."""

    report_dir: str = "reports/re-agent"
    log_dir: str = "reports/re-agent/logs"
    session_file: str = "re-agent-progress.json"
    format: str = "json"


@dataclass
class ReAgentConfig:
    """Top-level configuration for the re-agent system."""

    project_profile: ProjectProfile = field(default_factory=ProjectProfile)
    llm: LLMConfig = field(default_factory=LLMConfig)
    backend: BackendConfig = field(default_factory=BackendConfig)
    parity: ParityConfig = field(default_factory=ParityConfig)
    orchestrator: OrchestratorConfig = field(default_factory=OrchestratorConfig)
    output: OutputConfig = field(default_factory=OutputConfig)
    agents: AgentModelsConfig = field(default_factory=AgentModelsConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)

    @classmethod
    def create_default(cls) -> ReAgentConfig:
        """Create a configuration with all default values."""
        return cls(
            project_profile=ProjectProfile(),
            llm=LLMConfig(),
            agents=AgentModelsConfig(),
            backend=BackendConfig(),
            parity=ParityConfig(),
            orchestrator=OrchestratorConfig(),
            validation=ValidationConfig(),
            output=OutputConfig(),
        )
