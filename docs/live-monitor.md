# Live progress monitor

`re-agent monitor` serves a local dashboard independently of the reversal worker. It reads existing session JSON files and logs; it does not import model providers or start work automatically.

```sh
re-agent monitor --work-dir /path/to/project --session-glob re-agent-progress.json --total 500
```

Open the printed `http://127.0.0.1:8765` address. The page refreshes every two seconds and shows saved function results, review passes, failures, rounds, and recent log output. Omit `--total` when the planned count is unknown. A review pass is not a proof of compilation or binary equivalence.

Session patterns are relative to `--work-dir` and can be repeated. For a batch runner:

```sh
re-agent monitor --work-dir /path/to/project --session-glob 'reports/batch-*/progress.json' --log-glob 'reports/batch-*/stderr.log' --total 500
```

The monitor deduplicates function addresses across matching files using their latest result timestamps. Select sessions for the same binary/version; unrelated projects can reuse addresses. Unchanged files are cached, partial JSON writes retain the last readable snapshot, and log output is bounded to the last 7,000 bytes. Counters describe saved final results, not in-flight rounds.

## Optional worker controls

Install the optional process-management dependency:

```sh
pip install 'auto-re-agent[monitor]'
```

Supply an explicit argument array after `--worker`, which must be the last monitor option:

```sh
re-agent monitor --work-dir /path/to/project --session-glob re-agent-progress.json --total 500 --worker re-agent --config re-agent.yaml reverse --manifest targets.json --max-functions 500
```

On Windows, quote paths containing spaces and use an executable such as `python.exe` or `re-agent.exe`, rather than a shell script. Worker arguments are passed directly, without shell interpretation.

- **Start / resume** launches the configured command. Resuming completed work depends on that command's checkpoint behavior. ReAgent uses its configured session and attempt limits; the monitor does not reset them.
- **Stop run** terminates the owned worker process tree. Saved session files remain intact. An interrupted model call may need to be repeated.
- Closing the browser or monitor host leaves the worker running. Restart the monitor with the same work directory, state directory and worker command to reconnect.
- Duplicate starts sharing a state directory are protected by a process lock. Adoption checks the command, working directory record, PID and process creation time, so stale PID records do not attach to unrelated processes.

Control records and worker stdout/stderr live in `reports/monitor` by default; override with `--state-dir`. Treat these as local run artifacts, not source files. `--port` changes the port; zero selects an available port.

## Local access

The server binds only to IPv4 loopback. Host-header checks reject DNS-rebinding requests. Control endpoints require a random per-host token and reject cross-origin requests. HTTP clients cannot supply a different worker command or arbitrary file path. No external scripts, fonts, telemetry or services are used by the dashboard. This is a local tool, not a multi-user network service.
# External batch progress

Use the existing dashboard for a separately launched batch runner:

```console
re-agent monitor --work-dir /path/to/run --progress-file status.json --stop-file STOP --log-glob "batch-*/stderr.log"
```

Both file paths must stay within the working directory. The monitor does not adopt
or restart the external process. Stop creates the configured cooperative signal;
the runner must watch it and cancel its own children. Omit `--stop-file` for
read-only reporting. External progress cannot be combined with `--worker`.

The progress JSON uses `phase`, Unix-second `started`, `updated` and
`batch_started` timestamps, and integer `total`, `completed`, `compiled`, `failed`,
`verified`, `batch`, `batches`, `child_started`, `child_returned` and
`active_children` counters. `recent` contains rows with `address`, `compiled` and
optional `diagnostic`; optional `error` describes a run-level failure.
Active phases are `opening-analysis`, `exporting-evidence`, `native-subagents` and
`validating-candidates`. Updates older than 60 seconds are marked stale; this is
a freshness indication, not proof that a process has exited.

Compiled drafts are explicitly distinguished from accepted reconstructions.
Child counts describe starts and collected results, not measured model-request
concurrency. The existing layout displays elapsed time, throughput, batch
progress, diagnostics and source-data age. The browser receives an initial snapshot over `/api/stream`, followed by WebSocket
updates. Reconnects load a fresh snapshot; no periodic browser status requests are used.
The host checks local progress files every 500 ms. Controls remain authenticated HTTP POSTs.

Use `--event-glob "batch-*/native.jsonl"` to enable the agent workspace for native
Grok event logs. Select the live batch or an earlier batch, then an agent to inspect
its code, full response, and tool activity. Unattributed token events stay in a
shared stream; child results are assigned only by provider task IDs. Thought events
are excluded. Each agent text/log tail is bounded to 64 KiB, with at most 128 recent
agents retained. Historical reads are limited to 32 MiB per source. The source must
match the configured pattern inside the run directory.

## Native Windows session storage

Keep native CLI session storage separate from deeply nested report directories.
Grok embeds the encoded working directory and two session IDs in child output
paths. On Windows, native callers can pass an explicitly allocated short `home`
to `GrokCLIProvider._isolated_environment(..., native_subagents=True)`. The helper
checks a conservative 240-character output-path budget before creating the home.
It preserves the original auth path and managed requirements without copying
credentials. The caller owns the separate home and its retention/cleanup. Use a
new isolated directory for a clean run; existing homes are never overwritten.
