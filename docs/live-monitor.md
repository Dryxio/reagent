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
