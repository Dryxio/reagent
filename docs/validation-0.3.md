# Validation of 0.3.0

The regression suite covers compiler-error repair, runtime-error repair, semantic-rule repair, dependency edges, cumulative builds, overload refusal, Clang namespaces/operators/UTF-8 offsets, bridge contracts, symlink handling, shell quoting, process timeouts, call budgets, checkpoint recovery, input invalidation, differential observables and CLI diagnostics.

The suite contains 131 tests: the original 91 plus 40 regression/integration cases. All pass locally on Python 3.13; Ruff and strict mypy pass. Unit tests use deterministic providers. Compiler integration tests invoke the host C++ compiler and the optional Clang indexer.

## GTA San Andreas reference experiment

On 2026-09-04, three functions from a local `gta-reversed-dryxio` checkout were generated with Claude CLI (`sonnet`) and independently reviewed in a separate Claude conversation. Each candidate passed in one review round, compiled through the minimal timer adapter in `examples/gta_timer`, and matched the original x86 code under Unicorn.

| Function | Address | Observable | Cases |
|---|---|---|---|
| CTimer::GetCyclesPerMillisecond | 0x561A40 | EAX return / uint32 divider | 7 |
| CTimer::StartUserPause | 0x561AF0 | Pause byte written at 0xB7CB49 | 7 |
| CTimer::EndUserPause | 0x561B00 | Pause byte written at 0xB7CB49 | 7 |

Inputs: `0, 1, 2, 20, 1000, 2147483647, 4294967295`. For pause functions, the initial state uses the low bit. The divider is written at 0xB7CB2C. The emulator checks that each function returns to its sentinel within 100 instructions and a one-second execution limit.

Reference executable SHA-256: `72ae59e44c761389e354a50dc6215e964fe771121e2f4b1877273a493ceecc9b`.

These are 21 matching observations, not exhaustive verification. All three explicit wrong-output controls were detected in the benchmark replay (6/6 expectations met). See [machine-readable results](benchmark-0.3.json). Source files in the reference checkout were not changed. The adapter validates the selected function, not full GTA linking, scheduling, graphics or other runtime behavior. Source declarations supply field names; the machine code supplies the differential oracle.

No copyrighted reference binary/export is bundled. Install `auto-re-agent[benchmark]` for the optional emulator dependencies. Provide your own matching binary and exports. See the example README for reproduction.
