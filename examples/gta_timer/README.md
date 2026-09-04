# GTA timer differential adapters

These small adapters compare three timer leaf functions against a user-owned GTA SA PE image. They are intentionally explicit about addresses, ABI and observables. No binary is supplied.

Install `pip install 'auto-re-agent[benchmark]'` and a C++ compiler. `reference.py EXE ADDRESS` reads one JSON integer, runs the original x86 function under Unicorn and emits the observable return or pause byte. `build_candidate.py SOURCE OUTPUT METHOD` extracts and compiles the overlaid timer method with a minimal declaration shim.

Configure a target project with absolute paths:

```yaml
backend:
  type: ghidra-json
  export_dir: /path/to/ghidra-exports
project_profile:
  source_root: /path/to/gta-reversed/source/game_sa
llm:
  provider: claude-cli
  model: sonnet
validation:
  enabled: true
  trust_configured_commands: true
  build_commands:
    - python /path/to/examples/gta_timer/build_candidate.py "{candidate_file}" "{overlay_root}/program" GetCyclesPerMillisecond
  differential_reference:
    - python
    - /path/to/examples/gta_timer/reference.py
    - /path/to/gta_sa_compact1.0.exe
    - '0x561A40'
  differential_candidate: ['{overlay_root}/program']
  differential_cases_file: /path/to/cases.json
```

Use `cases.json` containing `[0, 1, 2, 20, 1000, 2147483647, 4294967295]`, then run `re-agent doctor --address 0x561A40` and `re-agent reverse --address 0x561A40`. The standard GTA hook patterns resolve the original method name. Select `StartUserPause`/`0x561AF0` or `EndUserPause`/`0x561B00` by changing both the build adapter method and reference address.

These commands validate one extracted method. They do not build the full game, modify the original source tree, or prove behavior outside the supplied cases.
