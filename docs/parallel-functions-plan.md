# Bounded parallel function processing

Status: implemented. This document preserves the staged design and intermediate
constraints. See [the usage guide](parallel-functions.md) for the final supported
configuration, including cumulative promotion and parallel validation.

## Objective

Reduce elapsed time for independent function reconstruction while preserving the
same acceptance gates, per-function model budgets, checkpoints, and stop controls.
Scheduling belongs to ReAgent and must work with all providers registered through
`llm/registry.py`, including API providers and Claude, Codex, and Grok CLI providers.
This is separate from a provider's internal subagent or workflow features.

## Existing integration points

- `orchestrator/class_runner.py` calls `pick_next` and `reverse_single` sequentially.
  Successful cumulative candidates are promoted into one scratch project and the
  source index is rebuilt before subsequent work.
- `orchestrator/batch_runner.py` adapts manifests to the class runner. Both entry
  points should use one scheduler rather than separate parallel implementations.
- `orchestrator/single.py` and `agents/loop.py` own the existing function pipeline,
  validation, repair rounds, logs, checkpoints, and final result recording.
- Provider instances hold conversation state and mutable `last_metadata`. They
  must not be shared by concurrently running functions.
- `core/session.py` protects individual writes with file locks, but selecting a
  target and claiming it are not one transaction. Locks alone do not prevent two
  workers from selecting the same function or reading stale in-memory state.
- `core/function_picker.py` implements selection and dependency ordering. Reuse
  its ranking semantics; do not repeatedly traverse the graph for each free slot.
- `monitor/server.py` reads saved results and controls an owned worker tree. It
  currently has no per-function in-flight view.

## Initial contract

Proposed configuration, to be implemented and validated in stage 1:

```yaml
orchestrator:
  max_parallel_functions: 1
  max_parallel_validations: 1
```

Both are positive integers. Initial supported function concurrency is 1 through
32, with a default of 1; this is a local scheduling limit, not a claim about any
provider's entitlement or throughput. Validation concurrency cannot exceed
function concurrency. The first parallel release permits validation concurrency
of 1 only; higher values require stage 6's isolation checks.

Keep the current sequential path and public APIs working unchanged at concurrency
1. Existing callers that supply already-created providers may use that path.
Parallel callers must supply factories/configuration: never clone or share an
unknown live provider instance. Reject incompatible settings before any model call.

The first release supports independent draft reconstruction against a fixed input
snapshot. Reject parallel execution when both `validation.copy_project` and
`orchestrator.cumulative_validation` are true. Do not silently turn off cumulative
validation, weaken a gate, or promote speculative results. Stage 8 extends this.

## Scheduler and ownership

Use a bounded thread executor for orchestration: most work waits on model requests
and subprocesses. Threads avoid requiring every backend, provider, and test double
to be serializable. Cancellation of a future does not stop an executing thread;
explicit request/subprocess cancellation is a separate requirement below.

One coordinator owns target enumeration, normalized-address claims, attempt
accounting, final result publication, and durable session writes. Submit at most
the available worker slots instead of queuing the entire inventory into futures.
Never have two active attempts for the same normalized address.

Each job owns fresh reverser/checker provider instances, conversation IDs, call
budget, configuration copy, source-index state, backend view, and artifact paths.
Use `<run>/<address>/<attempt-id>` isolation, including compiler working directories
and mutable parity caches. Audit lazy caches in indexers/backends rather than
assuming read methods are immutable. Provider resources must be closed after a
job on success, exception, or cancellation.

Share immutable exported evidence where possible. Initially serialize calls to
live/stateful backends through a coordinator-owned adapter. Allow independent
backend instances only after their transport/session isolation is tested. Never
issue concurrent operations against a shared Ghidra analysis session by default.

Worker checkpoints are typed events sent to the coordinator. A worker waits for
durable acknowledgement before claiming a checkpoint is saved. Prior feedback is
an immutable per-target snapshot. Workers do not mutate a shared `Session` object.
Keep the existing on-disk session result format readable by existing tools.

Acquire an exclusive coordinator lease for the run/session identity. A second
process targeting the same session must fail before dispatch. Release it on clean
exit; recover after process death using OS locking and recorded ownership, not a
stale PID alone. Use attempt IDs to ignore duplicate/stale completion events.

Return results in deterministic planned order, while publishing progress as jobs
finish. This stabilizes reports without blocking execution on an early slow job.

## Selection and dependency semantics

Prepare a normalized, deduplicated candidate inventory once. Apply completion and
attempt filters from the bound session and retain current strategy tie-breakers.
Refresh eligibility after each terminal attempt; never exceed the invocation's
function-attempt limit when retries are admitted.

For `dependency-order`, derive strongly connected components and schedule ready
components callee-first. Process targets within a cycle deterministically and
sequentially; independent ready components can run concurrently. External or
unselected callees are evidence dependencies, not jobs to invent automatically.

In fixed-snapshot draft mode, this order does not make a callee's generated source
available to its caller. A terminal failed callee does not automatically reject a
caller; record the outcome and let existing evidence/validation gates decide.
Document this distinction from cumulative source promotion.

## Cancellation, failures, and recovery

Stop closes dispatch immediately, marks queued targets unstarted, and requests
cancellation of active jobs. A shared cancellation token is checked before model
calls, repairs, validation commands, and result publication. Waiting on a semaphore
or backend lock must also be cancellable.

Extend the shared process runner with explicit cancellation and an owned-process
registry. Migrate CLI providers still using direct `subprocess.run` to this path.
Terminate child trees and reap processes on timeout/cancellation. POSIX subprocesses
may create their own sessions: killing only the coordinator's process group is
insufficient. Cover detached descendants on POSIX and process trees on Windows.
Keep the monitor's hard-stop fallback for an unresponsive coordinator.

For synchronous API requests that cannot be interrupted, stop dispatch immediately
and wait no longer than the configured request timeout. Discard late results from
cancelled attempts. Report `stopping` until cleanup finishes; never claim an
in-flight remote request was revoked if the provider cannot do that.

Persist `queued`, `running`, `completed`, `failed`, and `interrupted` attempt states
separately from acceptance verdicts. Interrupted jobs retain saved round feedback
and can resume without being mistaken for successful results. Keep a record of
calls already spent; resumed work must not reset the same attempt's call budget.
A subsequent deliberate attempt follows the existing attempt-limit policy.

One ordinary candidate rejection must not cancel independent jobs. Fatal shared
failures (authentication/configuration failure or inaccessible evidence source)
stop dispatch and record a run-level reason. Introduce typed failure categories;
do not infer authentication or rate limits from arbitrary diagnostic substrings.
Do not add an unbounded retry loop. Provider-specific rate-limit handling must
respect existing budgets and cancellation.

Separate scheduling settings from semantic acceptance identity: changing worker
count alone should not invalidate completed results. Evidence, source, model, and
acceptance-policy changes must continue to invalidate incompatible results.

## Live reporting

Add an atomically written, versioned execution-status file owned by the coordinator.
Keep final session results authoritative for completed/accepted counts and use the
status file for in-flight state. The monitor remains a decoupled reader.

Report the run/attempt IDs, concurrency limit, active target addresses, current
stage, elapsed times, queue count, compiler waits, model waits, stop state, and
completed/failed/interrupted counts. Reconcile old worker-stage data with live
process ownership so a killed worker cannot remain displayed as reconstructing.
Show review passes and validation outcomes separately.

Attach progress events and logs to job IDs; do not interleave unlabelled model
output. Preserve the existing single-worker display for older status/session files.

## Staged commits and required tests

Each stage is a separate implementation commit, with its own regression tests.
Keep intermediate stages disabled until their required infrastructure is present.

1. **Define parallel execution configuration and job contracts.** Add validated
   options, immutable job/event types, and provider/backend factory boundaries.
   Test YAML/per-role loading, invalid limits, unsupported cumulative mode, and
   exact compatibility of the default sequential path.
2. **Isolate function job resources.** Reuse `reverse_single` through a worker
   context and coordinator checkpoint sink. Test overlapping jobs with barriers:
   distinct conversations, metadata, budgets, artifacts, caches, and compiler
   working directories; cleanup on failures. Cover every registered provider with
   fake transports, without paid or network requests in CI.
3. **Add bounded scheduling and deterministic dependency selection.** Test active
   worker caps, duplicate address normalization, diamond graphs, cycles, external
   callees, retry/attempt limits, out-of-order completion, and stable output order.
   Use events/barriers rather than brittle wall-clock speed assertions.
4. **Make parallel checkpoints and resume durable.** Test simultaneous checkpoints,
   duplicate completion events, coordinator crashes, lease recovery, competing
   coordinators, preserved budgets, and worker-count-only configuration changes.
5. **Implement cancellation across workers and providers.** Test stop while queued,
   waiting for a lock, generating, compiling, and publishing. Use disposable real
   process trees on Windows/Linux/macOS, including POSIX detached descendants.
   Test late API replies and ensure Stop cannot promote or count cancelled results.
6. **Control validation concurrency.** Keep one shared validation lane by default.
   Opt-in parallel validation requires isolated writable roots and an explicit
   concurrency-safe command contract; isolated paths alone do not make external
   services or reference harnesses safe. Test common build filenames, shared
   reference commands, resource limits, and unchanged acceptance verdicts.
7. **Expose the scheduler through CLI and monitor.** Wire both manifest and class
   entry points. Test full fake-provider runs at 1/2/4 workers, status transitions,
   monitor reconnect, cancellation controls, and backwards-compatible sessions.
   Enable parallel mode only once stages 1–6 are integrated and passing.
8. **Support cumulative validation separately.** Use immutable source generations
   for parallel proposals and deterministic serial promotion. Revalidate each
   proposal against the latest promoted generation; reject stale candidates or
   return them to a bounded repair step. Test two candidates editing one source
   file, dependent promotions, validation failure, cycles, and crash recovery.
   Do not publish a proposal's provisional PASS before promotion/revalidation.

## Measurement and release criteria

Benchmark the same pinned evidence and target set at 1, 2, and 4 workers, using
fresh isolated outputs and the same models, prompts, budgets, and acceptance
rules. Compare wall time, summed model time, calls, compiler time, memory, rate
limits, interruptions, and acceptance results. Repeat trials to distinguish
provider variation from scheduler effects. Keep benchmark reports out of Git.

Release the independent parallel mode after cross-platform tests establish no
duplicate jobs, lost checkpoints, metadata cross-talk, orphaned local workers,
or acceptance changes caused by scheduling. Report measured throughput honestly;
do not promise linear scaling or enable 32 workers by default. Grok-managed
subagents and workflows remain a separate future design.
