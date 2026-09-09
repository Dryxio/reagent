# Configuration

re-agent is configured primarily through `re-agent.yaml`, plus the supported
environment variables and runtime CLI flags documented below.

## Priority Order

Supported CLI overrides > supported environment variables > YAML config > defaults

## Environment Variables

| Variable | Maps to |
|----------|---------|
| `RE_AGENT_LLM_PROVIDER` | `llm.provider` |
| `RE_AGENT_LLM_API_KEY` | `llm.api_key` |
| `RE_AGENT_LLM_MODEL` | `llm.model` |
| `RE_AGENT_LLM_BASE_URL` | `llm.base_url` |
| `RE_AGENT_BACKEND_CLI_PATH` | `backend.cli_path` |
| `RE_AGENT_BACKEND_TIMEOUT` | `backend.timeout_s` |

## LLM Config

```yaml
llm:
  provider: "claude"        # claude | claude-cli | openai | openai-compat | codex
  model: "claude-sonnet-4-5-20250929"
  api_key: null
  base_url: null
  max_tokens: 4096
  temperature: 0.0
  timeout_s: 1800
  input_cost_per_million: 0.0
  output_cost_per_million: 0.0
```

Notes:

- `claude` uses the Anthropic SDK and typically reads `ANTHROPIC_API_KEY`
- `openai` and `openai-compat` use the OpenAI-compatible chat completions provider and typically read `OPENAI_API_KEY`
- `codex` uses the local `codex` CLI and ChatGPT login credentials instead of an API key
- `claude-cli` uses the local Claude Code CLI login. `cli_path`,
  `max_budget_usd`, and `effort` are optional.

Independent role overrides inherit the top-level `llm` block only when the
role is omitted:

```yaml
agents:
  reverser:
    provider: claude-cli
    model: sonnet
    max_budget_usd: 1.0
    effort: high
  checker:
    provider: codex
    model: gpt-5.4
```

A present role block is a complete `LLMConfig`; its individual fields are not
merged with the top-level block.

## Backend Config

```yaml
backend:
  type: ghidra-bridge
  cli_path: ghidra-bridge
  timeout_s: 45
```

The `ghidra-ai-bridge` package installs the `ghidra-bridge` executable. Prepare
its exports separately before running reversal commands.

## Project Profile

The `project_profile` section makes re-agent work across different RE projects.
This example is specifically for GTA-reversed-style source:

```yaml
project_profile:
  hook_patterns:
    - 'RH_ScopedInstall\s*\(\s*(\w+)\s*,\s*(0x[0-9A-Fa-f]+)'
  stub_markers: ["NOTSA_UNREACHABLE"]
  stub_call_prefix: "plugin::Call"
  source_root: "./source/game_sa"
  source_extensions: [".cpp", ".h", ".hpp"]
```

## Parity Config

```yaml
parity:
  enabled: true
  call_count_warn_diff: 3
  inline_wrapper_autoskip: false
```

## Orchestrator Config

```yaml
orchestrator:
  max_review_rounds: 4
  max_functions_per_class: 10
  objective_verifier_enabled: true
  objective_call_count_tolerance: 3
  objective_control_flow_tolerance: 2
  investigation_enabled: true
  max_investigations: 8
  selection_strategy: dependency-order # dependency-order | easiest-first | high-impact
  max_attempts_per_function: 3
```

## Candidate Validation

Generated code is written to a safe overlay. Commands can use both format
placeholders and environment variables.

```yaml
validation:
  enabled: true
  # true copies the project to a temporary isolated directory before commands
  copy_project: false
  project_root: .
  build_commands:
    - 'clang++ -fsyntax-only "{candidate_file}"'
  test_commands: []
  runtime_commands: [] # optional differential/record-replay harness
  require_build: true
  require_tests: false
  require_runtime: false
  require_verified: true # UNKNOWN validation results block acceptance
  trust_configured_commands: false # explicit attestation for project-owned shell gates
  parity_fail_on_red: true
  parity_fail_on_yellow: false
  command_timeout_s: 900
  working_directory: .
  keep_project_copy: false # delete isolated full-project copies after validation
```

With `copy_project: true`, the default working directory becomes the isolated
project copy and the source candidate replaces the real relative source file.
Configure the project inside that copy (for example `cmake -S . -B build`)
before building because generated `build/` directories are intentionally not copied.
With it disabled, build commands must consume `{candidate_file}` or
`RE_AGENT_CANDIDATE_FILE` explicitly.

Shell commands are user-defined and cannot be proven meaningful merely by
inspecting their text. They therefore produce `UNKNOWN` until
`trust_configured_commands: true` explicitly attests that the configured
project commands compile/test the candidate. The non-isolated placeholder
check is an additional mistake detector, not a semantic proof.


## Version 0.3 options and migration

`orchestrator.max_llm_calls_per_function` (default 80) is a shared cap across all
reverser and checker calls, including investigation responses. Each review round
can use up to `max_investigations` evidence actions. `estimate` reports that bound;
token counts remain planning estimates, not provider guarantees.

`orchestrator.cumulative_validation` defaults to true and takes effect with
`validation.copy_project: true`. Successful candidates are promoted only into the
scratch project so later candidates validate against them. Source files in the
original project remain unchanged.

For structured exports, select:

```yaml
backend:
  type: ghidra-json
  export_dir: /path/to/ghidra-exports
  address_map: /path/to/address-map.json # optional
project_profile:
  compilation_database: /path/to/compile_commands.json # optional, requires clang++
```

The JSON backend reads bridge export objects directly (schema 1, including legacy
unversioned exports). Missing required function data and unsupported versions fail
explicitly. Class enumeration includes matching exported functions and has no
50-item display limit; session state filters accepted functions. Provide an address
map to associate unnamed `FUN_*` exports with source symbols.

Clang indexing uses translation unit compile flags and byte-correct body ranges.
Overloads without a unique identity, macro-expanded bodies and compiler errors are
rejected. The default lightweight indexer is still available without Clang.

Differential harness commands use argument arrays, without shell expansion. Each
harness reads one JSON value from stdin and emits one JSON value describing the
observed behavior. Include return values, modified memory and relevant effects in
that result. Comparison is exact; normalize address-dependent values or floating
point representations in the project-owned adapters when appropriate.

```yaml
validation:
  differential_reference: [/path/to/reference-harness]
  differential_candidate: ['{overlay_root}/candidate-harness']
  differential_cases_file: /path/to/cases.json
  trust_configured_commands: true
```

The cases file must be a nonempty JSON array. Timeouts, invalid JSON, nonzero exits
and mismatches fail the gate and are fed back to the reverser. A matching harness
only establishes agreement on its declared observables and supplied cases.

A benchmark manifest is a JSON array of objects with `name`, `reference`,
`candidate`, and `cases`. Optional `expected_match: false` marks a negative control;
`timeout_s` defaults to 30. The command fails when an expectation is not met.

CLI reversal now honors `output.format` (json, markdown or text), defaults to JSON,
and validates the acceptance configuration before model calls. Standalone parity
continues to support human overrides; candidate validation never inherits those
manual overrides. Session corruption raises an error without replacing the file.
Changes to fingerprinted inputs archive previous results; use a separate session
file for independent experiments. Round checkpoints seed subsequent attempts with
previous code and diagnostics; provider-side conversations are not resumed across
process restarts.

Shell gates require POSIX `/bin/sh`. Placeholders are expanded as environment data,
including in single/double quotes. Isolated working directories cannot escape the
copy. Copies are not OS sandboxes: trusted project commands can still explicitly
access external paths. Internal links are remapped; external/broken links fail.


### Portable validation commands

Each build/test/runtime command may be a legacy POSIX shell string or an argument
array. Arrays execute directly, without a shell, on Windows and POSIX:

```yaml
validation:
  build_commands:
    - [cmake, -S, "{overlay_root}", -B, "{overlay_root}/build"]
    - [cmake, --build, "{overlay_root}/build"]
  test_commands:
    - [ctest, --test-dir, "{overlay_root}/build", --output-on-failure]
```

Arguments support `{candidate_file}`, `{overlay_root}`, and `{source_file}`.
Spaces and shell metacharacters remain literal data; `$VAR` and shell syntax are
not expanded in arrays. Legacy strings still require `/bin/sh`; doctor reports
when it is missing. Existing isolation and command-trust requirements still
apply. Non-isolated arrays must explicitly include a candidate/overlay placeholder.


### Optional evidence gaps

Function-context bundles and per-function Ghidra JSON exports may include `gaps`:

```json
{"function":"0x140001000","site":"0x140001010","kind":"unresolved_call",
 "reason":"Indirect register call target not resolved","origin":"analysis-export"}
```

Kinds are `unavailable`, `unsupported`, `query_failed`, `unresolved_call`, and
`limit`. The site is optional. Addresses are hexadecimal strings of unrestricted
width. Labels describe observations; they do not establish recovered semantics.
The graph preserves full context bundles and gap records alongside existing
nodes/edges. Refreshing a context replaces its previous gap observations. Old
exports remain supported; absent caller/callee fields produce unavailable records
while explicitly empty lists describe a known empty result. Detailed indirect-call
records require an exporter that supplies them; ReAgent does not invent targets.
