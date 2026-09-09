"""Subprocess execution utilities."""

from __future__ import annotations

import subprocess
from collections.abc import Sequence


def run_cmd(args: Sequence[str], timeout_s: int = 45) -> tuple[bool, str]:
    """Run a command and return ``(success, combined_output)``.

    Args:
        args: Command and arguments to execute.
        timeout_s: Maximum wall-clock seconds before the process is killed.

    Returns:
        A tuple of ``(ok, output)`` where *ok* is ``True`` when the process
        exits with return code 0 and *output* contains combined stdout/stderr.
    """
    try:
        proc = subprocess.run(
            list(args),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode == 0, proc.stdout
    except subprocess.TimeoutExpired as e:
        return False, f"TIMEOUT after {timeout_s}s: {' '.join(str(a) for a in args)}\n{e}"
    except FileNotFoundError:
        return False, f"Command not found: {args[0]}"


def run_cmd_split(args: Sequence[str], timeout_s: int = 45) -> tuple[int, str, str]:
    """Run a command and return ``(returncode, stdout, stderr)`` separately.

    Unlike :func:`run_cmd`, this keeps stdout and stderr in separate streams
    so callers can inspect error messages independently of normal output.

    Returns ``(-1, "", error_message)`` on timeout or missing executable.
    """
    try:
        proc = subprocess.run(
            list(args),
            capture_output=True,
            text=True,
            timeout=timeout_s,
            check=False,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except subprocess.TimeoutExpired as e:
        return -1, "", f"TIMEOUT after {timeout_s}s: {e}"
    except FileNotFoundError:
        return -1, "", f"Command not found: {args[0]}"


def run_process(
    args: Sequence[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    input_text: str | None = None,
    timeout_s: float = 45,
    max_output_bytes: int = 1_048_576,
) -> subprocess.CompletedProcess[str]:
    """Bound captured output and terminate the entire process group on timeout."""
    import contextlib
    import os
    import signal
    import tempfile

    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        proc = subprocess.Popen(
            list(args),
            cwd=cwd,
            env=env,
            stdin=subprocess.PIPE,
            stdout=stdout,
            stderr=stderr,
            text=True,
            start_new_session=os.name != "nt",
        )
        try:
            import time

            from re_agent.orchestrator.execution import current

            context = current()
            deadline = time.monotonic() + timeout_s
            first = True
            while True:
                if context:
                    context.check()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(args, timeout_s)
                try:
                    proc.communicate(input_text if first else None,
                                     timeout=min(.1, remaining) if context else remaining)
                    break
                except subprocess.TimeoutExpired:
                    first = False
                    if not context:
                        raise
        except BaseException:
            # Capture descendants before killing their parent, including children
            # that have detached into a separate POSIX session.
            try:
                import psutil

                with contextlib.suppress(psutil.Error):
                    descendants = psutil.Process(proc.pid).children(recursive=True)
                    for child in reversed(descendants):
                        with contextlib.suppress(psutil.Error):
                            child.kill()
            except ImportError:
                pass
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True, check=False)
            else:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
            raise

        def read_tail(handle: object) -> str:
            # Temporary files bound memory even when a build emits a large log.
            import typing

            stream = typing.cast(typing.BinaryIO, handle)
            size = stream.seek(0, 2)
            stream.seek(max(0, size - max_output_bytes))
            text = stream.read().decode("utf-8", errors="replace")
            return ("[output truncated]\n" if size > max_output_bytes else "") + text

        return subprocess.CompletedProcess(list(args), proc.returncode, read_tail(stdout), read_tail(stderr))
