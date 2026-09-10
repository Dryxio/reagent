"""Grok Build CLI contract, based on native headless JSON responses."""
import json
import subprocess
from pathlib import Path

import pytest

from re_agent.config.schema import LLMConfig
from re_agent.llm.grok_cli import GrokCLIProvider, _validate_native_session_path
from re_agent.llm.protocol import Message
from re_agent.llm.registry import create_provider


def test_native_sessions_prompt_files_and_tool_free_arguments(monkeypatch):
    calls = []

    def fake(command, **kwargs):
        path = Path(command[command.index("--prompt-file") + 1])
        calls.append((command, kwargs, path, path.read_text(encoding="utf-8")))
        flag = "--resume" if "--resume" in command else "--session-id"
        cid = command[command.index(flag) + 1]
        return subprocess.CompletedProcess(command, 0, json.dumps({
            "text": "int f() { return 1; }", "stopReason": "end_turn", "sessionId": cid,
            "usage": {"input_tokens": 12}, "total_cost_usd": 0.01}), "")

    monkeypatch.setattr("re_agent.llm.grok_cli.run_process", fake)
    provider = create_provider(LLMConfig(provider="grok-cli", model="example-model", cli_path="grok path",
                                         effort="high", timeout_s=17))
    cid = provider.new_conversation("Preserve behavior")
    prompt = "Evidence π $() & " * 10000
    assert "int f()" in provider.resume(cid, prompt)
    provider.resume(cid, "Repair")
    first, second = calls
    assert first[3] == prompt
    assert prompt not in first[0]
    assert first[0][0] == "grok path"
    assert first[0][first[0].index("--tools") + 1] == "read_file"
    assert first[0][first[0].index("--deny") + 1] == "*"
    assert first[0][first[0].index("--disallowed-tools") + 1] == "read_file,search_tool,use_tool"
    assert first[1]["env"]["GROK_HOME"] != str(Path.home() / ".grok")
    assert not (Path(first[1]["env"]["GROK_HOME"]) / "auth.json").exists()
    assert "--no-subagents" in first[0] and "--disable-web-search" in first[0]
    assert first[0][first[0].index("--max-turns") + 1] == "1"
    assert first[0][first[0].index("--system-prompt-override") + 1] == "Preserve behavior"
    assert "--session-id" in first[0] and "--resume" not in first[0]
    assert "--resume" in second[0] and "--session-id" not in second[0]
    assert first[1]["timeout_s"] == 17
    assert first[1]["cwd"] == second[1]["cwd"] != str(Path.cwd())
    assert all(not call[2].exists() for call in calls)
    assert provider.last_metadata["usage"] == {"input_tokens": 12}


@pytest.mark.parametrize("payload", [[], {}, {"text": "", "stopReason": "end_turn"},
    {"text": "partial", "stopReason": "max_turns"},
    {"text": "bad", "stopReason": "end_turn", "is_error": True},
    {"text": "ok", "stopReason": "end_turn", "sessionId": "wrong-session"}])
def test_invalid_or_incomplete_responses_do_not_start_conversation(monkeypatch, payload):
    paths = []

    def fake(command, **kwargs):
        paths.append(Path(command[2]))
        return subprocess.CompletedProcess(command, 0, json.dumps(payload), "")

    monkeypatch.setattr("re_agent.llm.grok_cli.run_process", fake)
    provider = GrokCLIProvider()
    cid = provider.new_conversation("system")
    with pytest.raises(RuntimeError):
        provider.resume(cid, "prompt")
    assert provider._conversations[cid] == ("system", False)
    assert not paths[0].exists()


@pytest.mark.parametrize("failure, message", [
    (FileNotFoundError(), "not found"),
    (subprocess.TimeoutExpired("grok", 2), "timed out"),
    (subprocess.CompletedProcess([], 1, "", "login required"), "login required"),
    (subprocess.CompletedProcess([], 0, "not JSON", ""), "invalid JSON"),
])
def test_process_errors_and_prompt_cleanup(monkeypatch, failure, message):
    paths = []

    def fake(command, **kwargs):
        paths.append(Path(command[2]))
        if isinstance(failure, Exception):
            raise failure
        return failure

    monkeypatch.setattr("re_agent.llm.grok_cli.run_process", fake)
    with pytest.raises(RuntimeError, match=message):
        GrokCLIProvider().send([Message("system", "rules"), Message("user", "prompt")])
    assert not paths[0].exists()


def test_unknown_conversation_and_unsupported_budget():
    with pytest.raises(KeyError):
        GrokCLIProvider().resume("missing", "prompt")
    with pytest.raises(ValueError, match="max_budget_usd"):
        create_provider(LLMConfig(provider="grok-cli", max_budget_usd=1))


def test_native_path_budget_rejects_nested_home_but_accepts_short_home():
    workspace = r"D:\example-project\reports\native-full-20260910-104947\batch-0010"
    with pytest.raises(ValueError, match="shorter isolated home"):
        _validate_native_session_path(workspace + r"\grok-home", workspace)
    _validate_native_session_path(r"D:\rg\12345678\batch-0010", workspace)


def test_external_isolated_home_does_not_copy_auth_or_modify_workspace(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.mkdir()
    (original / "managed_config.toml").write_text("[models]\nmax_retries=0\n")
    monkeypatch.setenv("GROK_HOME", str(original))
    monkeypatch.setenv("GROK_AUTH_PATH", str(original / "auth.json"))
    workspace = tmp_path / "reports"
    workspace.mkdir()
    home = tmp_path / "isolated"
    env = GrokCLIProvider._isolated_environment(workspace, home=home)
    assert env["GROK_HOME"] == str(home)
    assert env["GROK_AUTH_PATH"] == str(original / "auth.json")
    assert not (home / "auth.json").exists()
    assert (home / "managed_config.toml").exists()
    assert not (workspace / "grok-home").exists()
    with pytest.raises(FileExistsError):
        GrokCLIProvider._isolated_environment(workspace, home=home)


def test_isolated_home_preserves_auth_location_and_local_requirements(tmp_path, monkeypatch):
    original = tmp_path / "original"
    original.mkdir()
    (original / "auth.json").write_text("DO NOT COPY")
    (original / "requirements.toml").write_text("[models]\nmax_retries = 0\n")
    monkeypatch.setenv("GROK_HOME", str(original))
    monkeypatch.delenv("GROK_AUTH_PATH", raising=False)
    provider = GrokCLIProvider()
    isolated = Path(provider._env["GROK_HOME"])
    assert provider._env["GROK_AUTH_PATH"] == str(original / "auth.json")
    assert not (isolated / "auth.json").exists()
    assert (isolated / "requirements.toml").read_text() == (original / "requirements.toml").read_text()
    assert "use_leader = false" in (isolated / "config.toml").read_text()
    provider.close()
    assert not isolated.exists()
    assert (original / "auth.json").read_text() == "DO NOT COPY"


def test_error_json_retains_native_stop_reason_and_usage(monkeypatch):
    payload = {"type": "error", "message": "max turns reached", "stopReason": "cancelled",
               "num_turns": 1, "usage": {"output_tokens": 100}}
    monkeypatch.setattr("re_agent.llm.grok_cli.run_process", lambda *args, **kwargs:
                        subprocess.CompletedProcess([], 1, json.dumps(payload), "secondary warning"))
    provider = GrokCLIProvider()
    try:
        with pytest.raises(RuntimeError, match="max turns reached"):
            provider.send([])
        assert provider.last_metadata["num_turns"] == 1
        assert provider.last_metadata["stopReason"] == "cancelled"
        assert provider.last_metadata["usage"]["output_tokens"] == 100
    finally:
        provider.close()
