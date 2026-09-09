# Changelog

## 0.4.0 — 2026-09-09

### Portable validation

- Accept argument arrays for build, test, and runtime gates. Execute them directly on Windows and POSIX, expanding candidate/source/overlay placeholders as literal argument data. This addresses native Windows validation in [#11](https://github.com/Dryxio/reagent/issues/11).
- Preserve legacy POSIX shell strings. Windows configurations must migrate to arrays; `doctor` now reports a missing `/bin/sh` when strings are configured.
- Return explicit failures when commands cannot start, and retain per-command build/test/runtime/differential outcomes in reports and sessions.
- Add Windows Python 3.12 CI alongside Linux and macOS coverage.

### Bounded function workflows

- Add `plan` to collect seed functions and direct callees within explicit depth and function limits, without model calls.
- Add `reverse --manifest` with dependency ordering, bounded retries, cross-class resume, and cumulative validation in disposable project copies.
- Add `evidence --manifest` for linked JSON packets and searchable TSV indexes, and `status --manifest` for inventory coverage and stale-result reporting.
- Preserve structured evidence gaps and full context bundles. Keep generated manifests loadable when backend exceptions have empty or whitespace-only messages.

### Source and provider fixes

- Normalize CRLF source offsets from Clang to match the source indexer's character offsets.
- Avoid estimating machine basic-block counts from C++ keywords in CFG verification; retain checks for wholesale removal of branching.
- Send Codex CLI prompts through UTF-8 stdin to support large prompts, and preserve conversation history when requests fail.

### Compatibility and limits

- Existing POSIX string commands remain supported. Arrays do not expand shell syntax or environment-variable references; use `{candidate_file}`, `{overlay_root}`, and `{source_file}` placeholders.
- Project-owned commands still require explicit trust before their successful exit codes count as validation proof under the configured policy.
- Manifest coverage describes only the selected inventory. It does not establish whole-program coverage or semantic equivalence.

## 0.3.0 — 2026-09-04

### Autonomous repair and validation

- Run candidate build, test, runtime, differential and semantic parity gates on every review round. Feed failures and counterexamples back into the next repair.
- Add JSON harness differential validation of project-defined return values, memory writes and effects, plus `re-agent benchmark` manifests and negative controls.
- Stop repeated identical failures and enforce a shared reverser/checker call budget per function. Reject unresolved investigation requests instead of treating them as C++.
- Order functions using actual call dependencies, with deterministic cycle handling. Validate accepted class candidates cumulatively in a disposable project copy and restore accepted candidates when resuming a class.

### Evidence and source identity

- Add `ghidra-json`, a direct, validated reader of Ghidra export files, avoiding display-output parsing and list display limits.
- Fix legacy bridge parsing of source struct offsets, known symbols, signatures and empty xref results.
- Add opt-in Clang AST indexing through `project_profile.compilation_database`, including namespaces, operators and UTF-8 source offsets. Ambiguous overloads are rejected.
- Protect free-function overloads and prevent class lookup from silently selecting an unrelated free function.
- Share structured binary/type evidence with the checker; bound large JSON evidence without producing invalid JSON.
- Detect mismatching simple constant-return bodies without claiming general semantic equivalence.

### Isolation and recovery

- Remap internal symlinks into project copies and reject external/broken links. Constrain isolated working directories to the overlay.
- Expand shell placeholders as quoted environment data. Kill validation process groups on timeout and bound captured output memory.
- Persist round checkpoints, generated code, diagnostic findings and hashes. Feed previous checkpoints into subsequent attempts.
- Add unique run directories and per-call prompt/response/error/usage logs, including investigation calls.
- Serialize session mutations with process locks and use unique atomic temporary files. Refuse corrupt session files rather than silently resetting them.
- Fingerprint source, JSON evidence and acceptance policy for CLI reversal; archive stale progress when inputs change.

### CLI, providers and release quality

- Add `re-agent doctor`; reject unusable verified-acceptance configuration before LLM calls.
- Correct per-round investigation estimates, use configured dry-run limits and enumerate actual class targets.
- Honor JSON/Markdown output for reversal, preserve explicit class/address metadata and surface enumeration failures.
- Honor Codex executable paths and API provider timeouts; collect API usage metadata.
- Extend CI with macOS, real compiler regression checks and wheel/prompt-resource smoke tests. Re-run validation before PyPI publishing.

### Compatibility and limits

- Version 0.2 configuration remains usable. CLI defaults now honor `output.format: json` for reversal.
- Previously ambiguous source replacements, escaping overlay directories and outgoing symlinks now fail explicitly.
- Clang indexing is optional and requires Clang plus a usable compilation database. The legacy indexer remains available.
- Differential verification covers the supplied harness observables and cases. It is not a formal equivalence proof or an automatic ABI adapter for arbitrary binaries.
- Validation shell commands require a POSIX environment and remain trusted project code; copying a project is not an OS sandbox.
- The GTA reference validation covers three timer leaf functions using explicit ABI adapters, not a full game build. No game binaries or Ghidra exports are distributed.
