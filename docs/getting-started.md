# Getting Started

Install `auto-re-agent` and the Ghidra bridge from PyPI:

```bash
python3 -m pip install --upgrade "auto-re-agent[headless]>=0.4.0"
```

## Prepare Ghidra

```bash
ghidra-bridge init
# Edit ghidra-bridge.yaml with the local Ghidra project and program.
ghidra-bridge export all
ghidra-bridge build-map # optional when source/hook patterns are configured
ghidra-bridge info
```

## Configure the agent

```bash
re-agent init --profile generic-cpp
```

Edit `re-agent.yaml` to configure:

- an authenticated LLM provider;
- `backend.cli_path: ghidra-bridge`;
- the project source paths and patterns;
- trusted build/test commands for candidate validation.

On native Windows, use argument arrays for build/test/runtime commands, such as
`[cmake, --build, "{overlay_root}/build"]`. String commands still require POSIX
`/bin/sh`. See [portable validation commands](configuration.md#portable-validation-commands).

Validation is strict by default. With no configured commands it returns
`UNKNOWN`, which `require_verified: true` rejects. Configure meaningful project
gates and set `trust_configured_commands: true`, or explicitly disable
validation for an unverified exploratory run.

## Run

```bash
# Start with one function
re-agent reverse --address 0x401000

# Then run a bounded class batch
re-agent reverse --class CTrain --max-functions 5

# Analyze existing source-tree parity
re-agent parity --address 0x401000 --strict-exit

# Inspect recorded progress
re-agent status
```

See the repository [README](../README.md) for providers, validation semantics,
profiles, outputs, safety notes, and the complete CLI overview.
