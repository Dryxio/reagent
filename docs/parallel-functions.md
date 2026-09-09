# Parallel function processing

Class and manifest reconstruction can use bounded concurrent workers with any
registered provider. The default remains the existing sequential pipeline.

## Configure a run

```yaml
orchestrator:
  max_parallel_functions: 4
  max_parallel_validations: 1
```

Or override the limits for an invocation:

```sh
re-agent --config project.yaml reverse --manifest targets.json --max-parallel-functions 4
re-agent --config project.yaml reverse --class Example --max-parallel-functions 2
```

Function concurrency accepts integers from 1 through 32. This is a ReAgent limit;
your provider's account, request limits, and available resources still apply.
`--max-functions` remains a total function-attempt limit, including retries.
`--address` remains a single-function operation.

To run multiple validation commands concurrently, explicitly opt in:

```yaml
orchestrator:
  max_parallel_functions: 4
  max_parallel_validations: 2
validation:
  copy_project: true
  parallel_safe: true
```

`parallel_safe` declares that your commands and reference harnesses can overlap
without conflicting through external services, absolute output paths, or shared
state. ReAgent gives each candidate its own writable project copy. A single
validation lane is the default. Invalid limits fail before dispatch.

## Execution and acceptance

Each function gets fresh reverser and checker providers, conversations, model-call
budgets, source-index state, artifact directories, and parity caches. Calls into a
shared backend are serialized, including promotion validation. Provider factories
must return fresh instances. Library users pass `provider_factory=create_provider`
to `reverse_class` or `reverse_manifest`; passing only an existing provider remains
supported at one worker.

Addresses are normalized and deduplicated. The coordinator admits only as many
jobs as there are slots and returns results in stable target order. Dependency
selection uses strongly connected components: callees reach a terminal outcome
before callers start, and members of a cycle run serially in deterministic order.
A failed callee does not automatically reject its callers. In independent draft
mode, generated callee code is not added to the source snapshot.

When `validation.copy_project` and `orchestrator.cumulative_validation` are both
true, workers propose changes against isolated snapshots of the accepted scratch
project. Promotion follows dispatch order. Each provisional success must pass the
same validation and parity gates against the latest accepted source before it is
promoted and published as successful. A rejected proposal consumes an attempt;
any retry uses the existing attempt and invocation limits. Original source files
are never the cumulative scratch project. Full project copies and serial promotion
can limit speedup for large projects.

## Checkpoints and recovery

The coordinator holds an exclusive session lease across identity binding,
selection, and publication. A competing coordinator fails without starting jobs.
Worker checkpoints and model-call reservations are acknowledged after atomic
storage. Only the coordinator writes final session results.

Per-attempt journals live below the configured report directory in
`parallel/<identity>/jobs/<attempt-id>.json`; worker artifacts are in sibling
attempt directories. Final publication is idempotent by attempt ID, including
recovery after a crash between journal and session writes. Interrupted attempts
retain completed rounds, previous feedback, and spent call budget. They resume
with the remaining budget; exhaustion records a failure instead of granting free
calls. A later attempt follows the normal attempt limit.

Worker counts do not change semantic identity. Changing source, evidence, model,
or acceptance policy invalidates incompatible accepted results. Changing other
per-attempt execution policy can start a new parallel journal. Do not change
input files during a run. Keep generated sessions, journals, source proposals,
and benchmark reports outside version control.

## Monitor and stop

An adjacent `<session>.execution.json` file reports concurrency, queue size,
per-job stages/calls/times, and aggregate attempt states. The monitor reads this
versioned file without importing the scheduler. Final session results remain the
authority for accepted counts. Only active jobs and the latest 32 terminal jobs
are included in the compact status payload; full history remains in the journals.

The monitor verifies the process ID and creation time before showing activity.
Dead workers display interrupted stages. Existing sequential/older sessions keep
their previous display, and review verdicts and validation outcomes are separate.

Stop first closes dispatch and requests cooperative cleanup. Waiting jobs and
local subprocesses observe cancellation; cancelled late results are discarded.
Synchronous API calls finish or reach their configured transport timeout. SDK
retries are disabled so they cannot silently spend calls beyond the recorded
budget. The monitor shows Stopping during cleanup; **Force stop** terminates the
owned process tree when a request is unresponsive. Ctrl+C/SIGTERM also request
orderly shutdown in parallel CLI runs. The CLI returns 130 after cancellation.

Authentication failures with explicit HTTP 401/403 status, provider configuration
failures, and inaccessible backend evidence stop dispatch and record a run-level
category. Ordinary candidate failures leave independent jobs running.

## Validation

The regression suite exercises 1/2/4-worker full fake-provider pipelines,
barrier-controlled overlap, unique providers/artifacts, retry caps, dependency
ordering and cycles, lease contention, checkpoint crash recovery, spent budgets,
late cancellation, real disposable subprocess trees, isolated validation commands,
cumulative promotion, and monitor stop/reconnect behavior. CI runs Linux, Windows,
and macOS jobs. Fake-provider timing checks measure scheduler overhead and overlap;
they do not predict real model throughput or reconstruction quality.

Grok Build uses this same scheduler. Grok-managed subagents are still disabled;
this feature does not enable a provider-specific swarm mode.
