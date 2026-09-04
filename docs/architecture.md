# Architecture

re-agent is structured as a layered pipeline:

```
CLI -> Config -> Orchestrator -> Evidence Loop -> LLM Providers
                      |              |
                      v              v
              Function Picker    RE Backend Protocol
                      |
                      v
       Candidate Overlay -> Build/Test/Differential -> Parity Gate -> Repair feedback
```

## Layers

- **CLI**: argparse entry points (init, reverse, parity, status, estimate)
- **Config**: YAML + supported environment/CLI overrides, project profiles
- **Orchestrator**: Single function or class-level auto-advance
- **Agents**: independently configurable Reverser + Checker with fix loop
- **LLM**: Protocol-based providers (Claude API/CLI, OpenAI-compatible, Codex CLI)
- **Backend**: RE tool abstraction with context, vtable, global, string,
  normalized P-code, and CFG capability flags
- **Parity**: 11-signal verification engine with scoring
- **Validation**: safe candidate overlay plus configurable build/test/runtime commands
- **Knowledge graph**: persistent calls, strings, and global relationships
- **Reports**: JSON/markdown output, session tracking


Every round produces a typed result and checkpoint. Candidate validation is a
callback inside the repair loop, rather than a one-shot step after LLM approval.
Providers share a call budget and emit one audit record per call. The class runner
uses a disposable cumulative project; promotion only occurs after acceptance.

The optional Ghidra JSON backend consumes typed export records directly. The
legacy CLI backend remains supported with corrected text compatibility. Optional
Clang indexing reads compile_commands.json and refuses ambiguous rewrites.

Differential verification is a project-owned harness interface. The bundled GTA
example demonstrates executing a few original x86 functions under emulation and
comparing them with compiled candidates; arbitrary ABI adapters are not inferred.
