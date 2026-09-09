"""Codex transport regression tests; no account or model calls required."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from re_agent.llm.codex_cli import CodexCLIProvider
from re_agent.llm.protocol import Message


def test_large_unicode_prompt_uses_utf8_stdin(tmp_path, monkeypatch):
    fake = tmp_path / "fake_codex.py"
    fake.write_text(
        "import pathlib, sys\n"
        "assert sys.argv[-1] == '-'\n"
        "prompt = sys.stdin.buffer.read().decode('utf-8')\n"
        "out = sys.argv[sys.argv.index('--output-last-message') + 1]\n"
        "pathlib.Path(out).write_bytes(prompt.encode('utf-8'))\n"
        "sys.stdout.buffer.write('\u8a3a\u65ad \u2713'.encode('utf-8'))\n",
        encoding="utf-8",
    )
    run = subprocess.run
    output_paths = []

    def invoke(args, **kwargs):
        assert sum(len(arg) for arg in args) < 4096
        assert kwargs.get("encoding") == "utf-8"
        assert kwargs.get("input") == expected
        output_paths.append(Path(args[args.index('--output-last-message') + 1]))
        return run([sys.executable, str(fake), *args[1:]], **kwargs)

    monkeypatch.setattr("re_agent.llm.codex_cli.subprocess.run", invoke)
    content = "\u65e5\u672c\u8a9e evidence & | > \" ' " * 5000
    expected = "[USER]\n" + content.strip()
    assert CodexCLIProvider().send([Message(role="user", content=content)]) == expected
    assert all(not path.exists() for path in output_paths)


@pytest.mark.parametrize("failure", ["model", "timeout", "missing"])
def test_cli_failures_preserve_diagnostics_and_clean_output(monkeypatch, failure):
    output_paths = []

    def invoke(args, **kwargs):
        output_paths.append(Path(args[args.index('--output-last-message') + 1]))
        if failure == "timeout":
            raise subprocess.TimeoutExpired(args, 7)
        if failure == "missing":
            raise FileNotFoundError("not installed")
        return subprocess.CompletedProcess(
            args, 1, "The selected model requires a newer version of Codex. \u8a3a\u65ad"
        )

    monkeypatch.setattr("re_agent.llm.codex_cli.subprocess.run", invoke)
    match = {"model": "requires a newer version", "timeout": "timed out after 7s", "missing": "CLI not found"}
    with pytest.raises(RuntimeError, match=match[failure]):
        CodexCLIProvider(timeout_s=7).send([Message(role="user", content="test")])
    assert all(not path.exists() for path in output_paths)
