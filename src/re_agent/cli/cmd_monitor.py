"""Serve a local progress dashboard without importing model providers."""
from __future__ import annotations

import argparse
import contextlib
from pathlib import Path


def cmd_monitor(args: argparse.Namespace) -> int:
    from re_agent.monitor.server import Monitor, make_server

    if not 0 <= args.port <= 65535:
        raise ValueError("Port must be between 0 and 65535")
    root = Path(args.work_dir).resolve()
    monitor = Monitor(root, root / args.state_dir, args.session_glob or ["re-agent-progress.json"],
                      args.log_glob, args.total, args.worker)
    with make_server(monitor, args.port) as server:
        print(f"Live monitor: http://127.0.0.1:{server.server_port}", flush=True)
        print("Closing the monitor does not stop the worker. Use Stop run to terminate it.", flush=True)
        with contextlib.suppress(KeyboardInterrupt):
            server.serve_forever()
    return 0
