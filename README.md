# ReAgent

For a decoupled local progress window with optional worker controls, see the [live monitor guide](docs/live-monitor.md).

[![PyPI](https://img.shields.io/pypi/v/auto-re-agent)](https://pypi.org/project/auto-re-agent/)
[![Python](https://img.shields.io/pypi/pyversions/auto-re-agent)](https://pypi.org/project/auto-re-agent/)
[![CI](https://github.com/Dryxio/reagent/actions/workflows/ci.yml/badge.svg)](https://github.com/Dryxio/reagent/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

ReAgent is an open-source AI reverse-engineering agent that uses Ghidra
and LLMs—including Claude, Codex, and OpenAI-compatible models—to reconstruct
and validate C/C++ functions from compiled binaries. It combines independent
reverser/checker models, agentic evidence gathering, candidate build and test
gates, structural verification, and parity analysis in one autonomous workflow.

Original pre-0.2 demo: [YouTube](https://youtu.be/zBQJYMKmwAs?si=emi1kDsJ81-2-tc3)

## Get started with your AI

**Give your AI this repo and tell it what you want to do.** It can check your setup, install the tools it needs, and walk you through anything that needs your help. Use an AI coding agent that can access files and run commands on your computer.

Copy this into your agent:

> Help me set up https://github.com/Dryxio/reagent. Read the README, check my installed tools and AI provider setup, and help me install and configure what's missing. Ask which program or project to work on. Start with one small function. Run the setup check (re-agent doctor), explain which AI provider will be used and any API costs, and set a small model-call limit. Then try to recreate that function in C/C++ and show me the result, what passed validation, and what still needs checking.

You'll need a program to analyze and a supported AI provider. Your agent can help connect Ghidra and use an existing authenticated Claude or Codex CLI, or an API setup.

**Setup agents:** follow the [setup workflow](docs/agent-setup.md). Detect the host OS and installed toolchain, generate portable validation commands, and migrate incompatible existing commands before starting reversal. The user should not need an OS-specific prompt or edit command syntax manually.

## What it does

```text
re-agent reverse --class CTrain
    │
    ├── Configuration (YAML + supported environment overrides + CLI flags)
    ├── Function selection (dependency-order | easiest-first | high-impact)
    ├── Source and binary context
    │   ├── decompile, xrefs, structs, enums, vtables, globals, and strings
    │   └── normalized high P-code, CFG, assembly, and nearby project source
    ├── Reverser → checker → fix loop (bounded rounds and investigations)
    ├── Conservative structural verifier
    ├── Candidate overlay
    │   └── configured build, test, and runtime gates
    ├── Candidate parity gate (GREEN | YELLOW | RED)
    └── Reports, per-call logs, round checkpoints, session history, and knowledge graph
```

The tool generates candidate C/C++ implementations; it does not patch the
original source tree automatically. A successful reversal can require four
independent conditions:

1. the LLM checker returns `PASS`;
2. the objective verifier finds no strong structural mismatch;
3. candidate validation satisfies the configured acceptance policy;
4. parity is not blocked by the configured RED/YELLOW policy.

This is conservative verification, not a proof of semantic equivalence.

## New in 0.4.0

Build, test, and runtime validation now support argument arrays that execute
directly on Windows and POSIX. On native Windows, convert shell strings to arrays;
legacy strings still require `/bin/sh`. `re-agent doctor` reports a missing shell.

- `re-agent plan` builds bounded function manifests without model calls.
- `re-agent reverse --manifest` reconstructs selected functions across classes,
  with dependency ordering and cumulative validation in an isolated project copy.
- `re-agent evidence --manifest` exports stored evidence into linked JSON packets and TSV indexes.
- `re-agent status --manifest` reports coverage, stale results, and individual validation checks.
- Evidence gaps remain explicit, and manifests stay readable after backend errors with empty messages.
- Clang indexing handles CRLF offsets; Codex CLI requests use UTF-8 stdin for large prompts.

See [migration and configuration](docs/configuration.md#portable-validation-commands)
and [the changelog](CHANGELOG.md).

## Manual setup

Prefer to install it yourself? Expand the instructions below.

<details>
<summary>Manual installation, configuration and examples</summary>

## Requirements

- Python 3.10+
- Git, for the current source installation
- Ghidra plus a configured
  [Ghidra Bridge](https://github.com/Dryxio/ghidra-bridge)
- At least one LLM setup:
  - Claude API: `ANTHROPIC_API_KEY`
  - OpenAI-compatible API: `OPENAI_API_KEY`
  - Claude CLI: an authenticated local `claude` command
  - Codex CLI: an authenticated local `codex` command

## Installation

Install the agent and its Ghidra query bridge from PyPI:

```bash
python3 -m pip install --upgrade "auto-re-agent[ghidra-bridge]>=0.4.0"
```

For headless Ghidra exports, install the bridge with its PyGhidra extra:

```bash
python3 -m pip install --upgrade "auto-re-agent[headless]>=0.4.0"
```

To install the latest development versions directly from GitHub instead:

```bash
python3 -m pip install --upgrade \
  "ghidra-ai-bridge @ git+https://github.com/Dryxio/ghidra-bridge.git@main" \
  "auto-re-agent @ git+https://github.com/Dryxio/reagent.git@main"
```

## Set up Ghidra evidence

Run these commands from the project you want to reverse:

```bash
# Create ghidra-bridge.yaml, then edit its Ghidra project/program paths
ghidra-bridge init

# Requires the bridge headless extra and a local Ghidra installation
ghidra-bridge export all

# Optional but recommended when reversed source/hook patterns are available
ghidra-bridge build-map

# Confirm that exports and configuration are visible
ghidra-bridge info
```

See the [bridge documentation](https://github.com/Dryxio/ghidra-bridge)
for its Ghidra, export, and source-map configuration.

## Quick start

Create a configuration in the target project:

```bash
# Recommended portable default
re-agent init --profile generic-cpp

# Other available profiles
# re-agent init --profile windows-x64
# re-agent init --profile gta-reversed
# re-agent init --profile openrct2
```

Running `re-agent init` without `--profile` preserves the original
GTA-reversed defaults. Prefer an explicit profile for new projects.

Then edit `re-agent.yaml`. At minimum, select an LLM, point the backend at the
installed bridge executable, set the source paths, and configure validation.

```yaml
llm:
  provider: claude-cli
  model: sonnet

# Optional: use a different provider/model for checking.
agents:
  checker:
    provider: codex
    model: gpt-5.4

backend:
  type: ghidra-bridge
  cli_path: ghidra-bridge

project_profile:
  name: generic-cpp
  language_standard: C++20
  source_root: src
  hooks_csv: null

orchestrator:
  max_review_rounds: 4
  investigation_enabled: true
  max_investigations: 8
  selection_strategy: dependency-order
  max_attempts_per_function: 3

validation:
  enabled: true
  copy_project: true
  project_root: .
  build_commands:
    - [cmake, -S, ., -B, build]
    - [cmake, --build, build]
  test_commands:
    - [ctest, --test-dir, build, --output-on-failure]
  require_build: true
  require_tests: true
  require_verified: true
  # This explicitly attests that the project-owned commands above are
  # meaningful validation gates. Leave false for untrusted commands.
  trust_configured_commands: true
  keep_project_copy: false
  parity_fail_on_red: true
  parity_fail_on_yellow: false
```

Validation is deliberately strict: with the generated defaults, no configured
commands produce `UNKNOWN`, and `require_verified: true` rejects that result.
For exploration without build validation, explicitly set
`validation.enabled: false`; such results are not build-verified.

Start with one function before launching a class run:

```bash
re-agent reverse --address 0x401000
re-agent reverse --class CTrain --max-functions 10
re-agent status
```

## LLM providers

### Claude API

```yaml
llm:
  provider: claude
  model: claude-sonnet-4-5-20250929
```

Set `ANTHROPIC_API_KEY` or `RE_AGENT_LLM_API_KEY`.

### Claude CLI

Authenticate the local Claude Code CLI first, then configure:

```yaml
llm:
  provider: claude-cli
  model: sonnet
  cli_path: claude
  effort: high
  max_budget_usd: 1.0
```

Claude CLI supports real session resume and reports usage/cost metadata. A
stale CLI login can still require re-authentication even when its auth-status
command reports a session.

### Grok Build CLI

Authenticate with `grok login`, then configure:

```yaml
llm:
  provider: grok-cli
  model: "" # Use Grok Build's configured model, or specify a model ID.
  cli_path: grok
  timeout_s: 600
```

Supports native session resume, JSON response validation, usage metadata,
and tool-free requests from an isolated working directory. Large evidence
prompts use temporary files. See [Grok Build configuration](docs/grok-build.md)
for supported settings and limits.

### OpenAI-compatible APIs

```yaml
llm:
  provider: openai # or openai-compat
  model: your-model
  base_url: https://your-endpoint.example/v1 # optional
```

Set `OPENAI_API_KEY` or `RE_AGENT_LLM_API_KEY`.

### Codex CLI

```yaml
llm:
  provider: codex
  model: gpt-5.4
```

Codex uses the authenticated local `codex exec` command. CLI-provider
`max_tokens` values are planning allowances, not hard output limits.

Omit `agents.reverser` or `agents.checker` to reuse the top-level `llm`
configuration for that role. A role block is a complete role configuration,
not a field-by-field merge with `llm`.

</details>

## Evidence and investigation

When supported by the backend, the reverser preloads a bounded evidence bundle
and can request additional read-only operations:

- `decompile`, `xrefs_from`, and `xrefs_to`
- `struct` and `enum`
- `vtable`, `global`, and `strings`
- `context`, normalized `pcode`, and `cfg`

Evidence bundle data is also ingested into
`reports/re-agent/knowledge-graph.json`, connecting functions, calls, globals,
and strings. Unsupported bridge capabilities degrade gracefully.

## Candidate validation

Generated code is written to an overlay. With `copy_project: true`, the project
is copied to a temporary directory, the candidate replaces the matching body
there, and commands run from that copy. `.git`, `.venv`, `build`, `reports`, and
Python cache files are not copied. Internal symlinks are remapped into the copy; external or broken links are rejected. Temporary project copies are deleted unless
`keep_project_copy: true`.

Commands may use:

- `{candidate_file}`, `{overlay_root}`, and `{source_file}` placeholders;
- `RE_AGENT_CANDIDATE_FILE`, `RE_AGENT_OVERLAY_ROOT`, and
  `RE_AGENT_SOURCE_FILE` environment variables.

Configured build/test/runtime commands are arbitrary project-owned shell
commands. The agent cannot prove from their text that they actually validate a
candidate, so they only become acceptance evidence when
`trust_configured_commands: true` is set explicitly.

If multiple C++ definitions match an overloaded method and the source cannot be
disambiguated, the overlay is rejected instead of replacing an arbitrary body.

## Verification and parity

The objective verifier runs on each review round. It compares generated code
with available decompile, assembly, CFG, and normalized high P-code evidence.
It returns `FAIL` only for strong mismatches; insufficient evidence returns
`UNKNOWN`.

The reversal pipeline runs the 11 built-in heuristic parity signals and configured
semantic rules against the generated candidate body on every round. RED is blocking by default; YELLOW can be made
blocking with `validation.parity_fail_on_yellow`.

The standalone command analyzes existing source: `re-agent parity` analyzes functions in
the existing source tree. It also supports semantic-rule files and manual check
overrides. Its process exit code remains zero on RED unless `--strict-exit` is
used.

The 11 built-in signals are:

| Signal | Level | Description |
|---|---|---|
| Missing source | RED | No source body was found |
| Stub markers | RED | Source contains a configured stub marker |
| Trivial stub | RED | Small plugin-call-heavy body with no control flow |
| Large ASM, tiny source | RED | Large disassembly with a very small source body |
| Plugin-call heavy | YELLOW | Plugin calls dominate the source body |
| Short body | YELLOW | Body has fewer than six lines |
| Low call count | YELLOW | Decompiled callees greatly exceed source calls |
| FP sensitivity | YELLOW | Assembly has FP-sensitive operations but source has no math tokens |
| Call-count mismatch | YELLOW | Source and assembly call counts differ beyond the configured threshold |
| NaN logic | YELLOW | Decompile indicates NaN-sensitive behavior missing from source |
| Inline wrapper | INFO | Source forwards to an internal implementation |

The built-in signal set is fixed; configuration exposes selected thresholds,
inline-wrapper behavior, semantic rules, and manual overrides rather than an
individual toggle for every signal.

## CLI reference

Global options must precede the subcommand, for example
`re-agent --config custom.yaml status`.

| Command | Purpose |
|---|---|
| `re-agent init --profile generic-cpp` | Create `re-agent.yaml` from a profile |
| `re-agent reverse --address ADDR` | Reverse one function |
| `re-agent reverse --class CLASS --max-functions N` | Reverse a bounded class batch |
| `re-agent reverse --class CLASS --dry-run` | Show a target plan without LLM calls |
| `re-agent reverse ... --max-rounds N --skip-parity` | Override loop/parity behavior |
| `re-agent parity --address ADDR --strict-exit` | Analyze an existing source function |
| `re-agent parity --filter REGEX --limit N --output report.json` | Filter and export parity results |
| `re-agent parity ... --skip-ghidra` | Run source-only parity signals |
| `re-agent status --class CLASS --format text` | Show session progress |
| `re-agent estimate --address ADDR` | Estimate one function |
| `re-agent estimate --class CLASS --limit N` | Estimate a class batch |

Use `re-agent <command> --help` for the exact option list.

## Working with function groups

For targets that span classes or have no recovered class names, create a bounded
manifest before running reconstruction:

```sh
re-agent plan --address 0x140001000 --max-depth 2 --max-functions 50 --output group.json
re-agent reverse --manifest group.json --dry-run
re-agent reverse --manifest group.json --max-functions 5
re-agent status --manifest group.json --format json
re-agent evidence --manifest group.json --output evidence-packets
```

Planning makes no model calls. Manifests retain returned evidence, direct call
edges, input fingerprints, and explicit gaps. Execution reuses the existing
validation and resume workflow; evidence export reads the stored snapshot only.
Use a new or empty export directory. Coverage describes the selected inventory
and configured acceptance policy, not proof of whole-program equivalence.

Build/test/runtime commands also accept argument arrays for native Windows and
POSIX execution; legacy shell strings retain their `/bin/sh` requirement. See
[configuration](docs/configuration.md) for command forms and manifest semantics,
and [implementation stages](docs/tooling-upgrade-plan.md) for scope and validation.

## Configuration precedence

The effective order is CLI runtime overrides, supported environment variables,
`re-agent.yaml`, then dataclass defaults. The currently supported environment
variables are:

- `RE_AGENT_LLM_PROVIDER`
- `RE_AGENT_LLM_API_KEY`
- `RE_AGENT_LLM_MODEL`
- `RE_AGENT_LLM_BASE_URL`
- `RE_AGENT_BACKEND_CLI_PATH`
- `RE_AGENT_BACKEND_TIMEOUT`

Role-specific `agents.*` configuration, validation, project profiles, parity,
and output paths should be configured in YAML.

See [docs/configuration.md](docs/configuration.md) for the complete schema.

## Profiles

- `generic-cpp`: portable C/C++ defaults
- `windows-x64`: Microsoft x64-oriented prompt rules
- `gta-reversed`: GTA-reversed hooks, stubs, source paths, and project rules
- `openrct2`: OpenRCT2-oriented hook/stub patterns

Profiles initialize project configuration; they do not replace bridge exports
or project-specific validation commands.

## Outputs

Default artifacts include:

- `reports/re-agent/code/`: final generated code per function
- `reports/re-agent/logs/`: unique run directories with per-call prompts, responses, provider metadata and round results
- `reports/re-agent/candidates/`: non-isolated candidate overlays
- `reports/re-agent/knowledge-graph.json`: persistent evidence graph
- `re-agent-progress.json`: current per-function state plus run history

The session file is atomically rewritten on save. Its `functions` map stores the
latest state per address, while its `runs` list preserves recorded attempts.

## How it compares

| Approach | Primary use | Evidence and validation | Workflow |
|---|---|---|---|
| Traditional decompiler | Translate machine code into analyst-readable pseudocode | Decompiler analysis; correctness is assessed manually | Function-by-function analysis |
| Interactive Ghidra AI or MCP assistant | Let an analyst ask questions and request Ghidra operations | Depends on the analyst, prompts, and connected tools | Human-directed conversation |
| `auto-re-agent` | Generate and validate candidate C/C++ implementations | Ghidra evidence, independent checker, structural checks, configured build/tests, and parity signals | Bounded autonomous reverser/checker pipeline with persistent reports |

`auto-re-agent` complements Ghidra rather than replacing it: Ghidra supplies
the program analysis, while the agent orchestrates evidence collection,
implementation, review, validation, and reporting. It is designed for
repeatable project-scale workflows, not just one-off decompiler chat.

## Frequently asked questions

### Is auto-re-agent a decompiler?

Not in the traditional sense. Ghidra performs the disassembly, decompilation,
and program analysis. `auto-re-agent` uses that evidence plus project source
context and LLMs to produce and validate candidate C/C++ implementations.

### Does it require Ghidra?

The full binary-backed reversal workflow currently uses Ghidra through
`ghidra-ai-bridge`. Existing source can be checked with source-only parity via
`re-agent parity --skip-ghidra`, but that mode has less evidence.

### Which LLM providers are supported?

Claude API, Claude CLI, OpenAI-compatible APIs, and Codex CLI are supported.
The reverser and checker can use different providers or models.

### Does it modify the original source tree?

No. Generated implementations are written to reports and candidate overlays.
When isolated validation is enabled, builds and tests run in a temporary copy
of the project.

### Can it prove that generated source is equivalent to the binary?

No. The checker, structural verifier, configured build/test gates, and parity
signals provide conservative evidence, not a formal proof of semantic or
binary equivalence.

### What binaries and projects can it analyze?

It can work with programs that Ghidra can import and that the bridge can export.
Useful reconstruction also depends on project-specific source context, types,
symbols, validation commands, and the evidence available in the target binary.

### How are LLM cost and run length controlled?

Review rounds, investigations, and attempts per function are bounded in the
configuration. Provider logs record available usage and cost metadata; actual
cost depends on the selected models, evidence volume, and target complexity.

## Safety and limitations

- re-agent does not commit or push generated code;
- candidate generation does not overwrite the original source tree;
- review rounds, evidence actions, and per-function attempts are bounded;
- prompt/response logs include internal evidence-loop calls in unique run directories;
- validation argument arrays run directly on Windows and POSIX; shell strings
  require `/bin/sh`. Commands should only be trusted when controlled by the project owner;
- structural and parity checks catch useful mismatches but do not prove binary
  equivalence;
- real Ghidra/PyGhidra integration depends on the local Ghidra project and has
  to be tested in that environment.

## Why ghidra-ai-bridge stays separate

`ghidra-ai-bridge` remains an independent analysis package with a versioned
JSON/CLI evidence surface. auto-re-agent consumes it through a capability-based
backend, leaving room for future IDA, Binary Ninja, or other backends.

## Development

```bash
git clone https://github.com/Dryxio/reagent.git
git clone https://github.com/Dryxio/ghidra-bridge.git
cd reagent

python3 -m venv .venv
source .venv/bin/activate
python3 -m pip install -e "../ghidra-bridge[headless]"
python3 -m pip install -e ".[dev]"

pytest -q
ruff check src tests
mypy src
```

## License

MIT
