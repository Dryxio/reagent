# Setup workflow for AI agents

Use this workflow when a user asks you to install, configure, or upgrade ReAgent.
The normal setup prompt is sufficient on Windows, Linux, and macOS. Detect the
environment and adapt configuration as part of setup.

## Inspect the environment and project

- Detect the host OS from the Python runtime and locate the available Python,
  Ghidra bridge, AI provider, compiler, and build tools.
- Read existing ReAgent configuration and the project's own build instructions
  before generating or changing commands.
- Distinguish the host OS from the target binary's ABI. The `windows-x64` profile
  describes binary assumptions; it does not identify the machine running ReAgent.
- Ask the user only for information you cannot discover, such as the intended
  target project, or for interactive sign-in or installation steps requiring them.

## Configure validation for the detected tools

Prefer argument arrays for every host OS. They run directly, so ordinary native
Windows validation does not need `/bin/sh` or `cmd.exe` placeholder expansion.
Choose commands that build and test the actual candidate in the actual project.

For a CMake project using an isolated project copy, for example:

```yaml
validation:
  copy_project: true
  project_root: .
  build_commands:
    - [cmake, -S, "{overlay_root}", -B, "{overlay_root}/build"]
    - [cmake, --build, "{overlay_root}/build"]
  test_commands:
    - [ctest, --test-dir, "{overlay_root}/build", --output-on-failure]
```

Adapt the generator, configuration, compiler options, and test arguments to the
project. On Windows, ensure any required Visual Studio toolchain environment is
available to the process that launches ReAgent. Use absolute paths or
`{overlay_root}` for executables built inside the overlay.

Each array item is one argument. Keep paths with spaces as one item; do not put
extra shell quote characters inside its value. Use `{candidate_file}`,
`{overlay_root}`, and `{source_file}` placeholders. Arrays do not expand `$VAR`,
`%VAR%`, pipes, redirections, or other shell syntax.

When upgrading an existing configuration:

1. Inspect all build, test, and runtime commands. Identify their intended behavior
   and required executables.
2. If shell strings require an unavailable shell, migrate them to argument arrays
   or an appropriate project-owned script. Preserve the build options and checks.
   Do not mechanically split shell expressions into tokens: that can change their
   meaning or make a command silently skip its input.
3. Represent independent build steps as separate commands. Resolve environment
   setup and compound shell operations using the detected toolchain's supported
   interface or a suitable script.
4. Preserve existing acceptance settings. Do not disable validation, remove a
   required gate, or turn a failing command into a no-op to make setup pass.

Legacy POSIX string commands remain supported where `/bin/sh` is available.
The ReAgent runner executes the configured commands; it does not automatically
rewrite an existing project's configuration. The setup agent performs this
migration, without requiring a special instruction from the user.

## Verify before starting model work

Run `re-agent doctor` and fix its actionable setup findings before starting
reversal. If the toolchain environment changes, rerun it from that environment.

Check that the configured build gate consumes the candidate and rejects invalid
source in a disposable copy. Where runtime or differential gates are configured,
verify that a known failing input is rejected. Do not modify the original source
tree for these checks.

Enable `trust_configured_commands` only when the project-owned commands provide
meaningful validation under the user's intended policy. Then start with one
small function and bounded model calls, and report the actual checker, objective,
candidate validation, and parity results.

See [configuration and migration](configuration.md#portable-validation-commands)
for command syntax and validation behavior.
