# General-purpose tooling upgrades

Status: all six stages implemented on `feature/general-tooling-upgrades`.
Baseline inspected: cdf5a1f.

Purpose: improve ReAgent for larger binary-to-source projects through small,
independently reviewable changes that follow the existing package structure.
Ideas come from inspecting an external executable-analysis toolkit; no external
game code, addresses, formats, assets, or runtime dependencies should be copied.

## Existing functionality to reuse

- `backend/protocol.py`, `backend/exports.py`: capability-based evidence access
  and structured Ghidra exports, including an optional address map.
- `core/knowledge_graph.py`: persistent function/global/string graph.
- `core/function_picker.py`: dependency ordering and selection strategies.
- `core/identity.py`, `core/session.py`: SHA-256 input fingerprints, checkpoints,
  prior results, and stale-session handling.
- `agents/source_context.py`: nearby headers/source and accepted prior candidates.
- `verification/candidate.py`: candidate overlays and configured validation.
- `verification/differential.py`: exact observable comparisons between two JSON
  harnesses; timeout, malformed-output, and mismatch failures.
- `orchestrator/class_runner.py`: bounded class runs and cumulative scratch builds.
- `reports/formatter.py`, `cli/cmd_status.py`: result formatting and run status.
- `utils/evidence.py`: bounded evidence with explicit truncation indication.

Do not add another decoder, graph database, caching framework, LLM loop,
differential engine, or progress database.

## Stage 1: Portable validation command execution

Gap: build/test/runtime shell gates hardcode `/bin/sh`; the subprocess utility
already supports Windows and differential commands already use argument arrays.

Add an explicit argument-vector form for build/test/runtime commands. Preserve
legacy shell-string semantics and fail clearly when that shell is unavailable.
Pass each argument separately, expanding supported placeholders as data. Extend
doctor to validate the selected execution mode before model calls.

Placement: `config/schema.py`, `config/loader.py`, `verification/candidate.py`,
`utils/process.py`, `cli/cmd_doctor.py`; document in `docs/configuration.md`.

Acceptance: a Python-based fixture runs on Windows and POSIX, including paths
with spaces and literal shell metacharacters; timeout and nonzero-exit behavior
remain correct; legacy configuration retains its behavior; missing shells are
reported in preflight. Reuse existing command trust/acceptance policy.

## Stage 2: Preserve evidence gaps as structured records

Gap: capability flags and truncation markers exist, but the graph currently
stores resolved edges and cannot represent an unresolved call site or explain
why a traversal omitted evidence.

Add optional typed gap records for backend-reported unresolved indirect calls,
unsupported operations, failed evidence queries, and traversal limits. Include
source function, call-site address when supplied, evidence origin, and reason.
Keep analyst labels separate from backend facts. Do not infer that an absent
edge means the function has no dependencies, or invent indirect targets.

Placement: `core/models.py`, `backend/protocol.py`, `backend/exports.py`,
`core/knowledge_graph.py`. Extend capability/schema handling compatibly; only
claim data that the backend actually supplies. More detailed Ghidra extraction
may require a separate upstream bridge change and must not block legacy use.

Acceptance: old export fixtures still load; an unresolved call remains visible
after graph save/reload; known-empty, unavailable, and failed evidence stay
distinguishable; addresses above 32 bits survive unchanged.

## Stage 3: Plan bounded function groups without LLM calls

Gap: reverse currently selects one address or a class. A subsystem can cross
classes or consist entirely of unnamed functions.

Add a read-only planning command accepting explicit seed addresses and optional
backend symbol-search patterns. Follow direct callee references with explicit
depth/function limits and emit a versioned target manifest. Capture seed labels
as navigation metadata, selection reasons, external dependencies, and gaps.
Use the existing backend protocol and graph. Do not introduce guessed semantic
grouping, automatic class recovery, or whole-binary scanning in this stage.

Placement: new `core/target_plan.py`, new `cli/cmd_plan.py`, existing CLI wiring
and configuration. Manifest identity should reuse the existing fingerprint
machinery rather than introduce a competing identity system.

Acceptance: deterministic manifests; cycles terminate; duplicate seeds collapse;
depth and count boundaries report omitted work; backend errors remain visible;
the command never initializes an LLM provider. Use synthetic graph fixtures.

## Stage 4: Reverse an explicit target manifest

Gap: existing class orchestration cannot directly execute a group spanning
unrelated or unnamed functions.

Add manifest input to reverse. Extract only the shared batch mechanics from the
class runner, preserving its public behavior. Run manifest targets through
`reverse_single`, existing session state, call budgets, validation, and dependency
selection. Limit execution to manifest membership; show out-of-group dependencies
without silently adding work. Detect incompatible manifest inputs before calls.

Placement: `cli/cmd_reverse.py`, `orchestrator/class_runner.py`, a small shared
`orchestrator/batch_runner.py` if needed, and `core/function_picker.py`.

Acceptance: a mixed-class fixture runs in dependency order, resumes correctly,
respects function limits, and preserves failed/unattempted outcomes; existing
address/class CLI tests still pass. No separate retry or session implementation.

## Stage 5: Export searchable evidence packets

Gap: result reports and a graph JSON file exist, but there is no indexed evidence
packet exporter for a planned group of functions.

Export existing stored evidence into stable per-function packets plus an index
and TSV facts for functions, calls, references, and gaps. Include input identity,
evidence origin, and links to available raw artifacts. Preserve full stored
evidence; apply prompt budgets only when preparing model context. Never present
missing or previously truncated evidence as a complete dump.

Placement: new `reports/evidence.py`, a thin CLI export command, existing graph
and storage utilities. This stage exports existing evidence without new model
calls or a second binary-analysis pass.

Acceptance: deterministic ordering; cross-links resolve; tabs/newlines in names
are escaped; unresolved/limited evidence is visible; a packet can be traced back
to its source record. Use tiny synthetic data, not retail binary dumps.

## Stage 6: Report group coverage and blockers

Gap: status reports reversal outcomes, but does not reconcile an explicit target
inventory with unattempted functions and evidence/dependency gaps.

Extend status for a target manifest. Report planned, unattempted, attempted,
accepted under current policy, failed, and stale results, plus observed external
dependencies and evidence gaps. Display checker/build/test/differential outcomes
separately using existing verdicts. Do not describe policy acceptance as proven
equivalence, or manifest coverage as whole-program completeness.

Placement: `cli/cmd_status.py`, `reports/formatter.py`, existing session/graph
readers. Derive reports rather than persist another status database.

Acceptance: every manifest function is accounted for exactly once in the primary
status totals; excluded functions do not affect the denominator; stale inputs
are explicit; optional/disabled validation is never reported as passed testing.

## Delivered validation

- Full local Windows/Python 3.12 suite: 169 passed, 9 skipped.
- Skips: five POSIX shell cases, three symlink privilege cases, and one POSIX
  process-group test. These retain coverage on supported CI environments.
- Ruff: `ruff check src tests examples` passes.
- Existing Linux CI type target: `mypy --platform linux src/re_agent` passes.
- Source archive and wheel build successfully; packaged CLI smoke checks cover
  planning, dry-run reversal, evidence export, and manifest status.
- Windows Python 3.12 added to the CI matrix; hosted CI runs after publication.
- Real compiler tests exercise repair, differential failures, and cumulative
  cross-class restoration without modifying the source project.

Implementation notes: evidence packets are structured JSON, with Markdown index
and TSV facts. Manifest execution restricts enumeration through a thin adapter
and reuses the existing class runner and selector. Dependency ordering queries
the real backend; the adapter does not replace analysis evidence with plan edges.
Source checkouts, game assets, and runtime-specific adapters remain outside scope.

## Delivery sequence and limits

Implement one stage per focused change. Stage 1 is independent; stages 2 and 3
provide the evidence and target model for stages 4–6. For each stage, first check
the then-current branch for overlapping upstream work, add meaningful regression
tests under the matching existing `tests/test_*` area, update affected docs, and
run the project's applicable checks. Keep old configuration and export formats
working unless an explicit migration is documented.

Use generic compiled toy programs or synthetic backend fixtures. Keep project
addresses, subsystem seeds, asset parsers, ABI harnesses, and engine-specific
code in consuming projects, outside ReAgent.

Defer automatic project scaffolding/type recovery, executable instrumentation,
asset extraction, and renderer replay. Those are larger separate designs and
should not be bundled into these tooling upgrades. Existing address maps, source
indexing, overlays, and differential hooks must be evaluated before proposing
any future extension in those areas.
