"""Exercise web references through the normal config, orchestration and MCP caller.

Model replies are scripted; the MCP SDK and HTTP request serialization are real.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from re_agent.agents.reverser import ReverserAgent
from re_agent.backend.stub import StubBackend
from re_agent.config.loader import load_config
from re_agent.config.schema import SearchConfig
from re_agent.core.models import FunctionTarget
from re_agent.llm.protocol import Message
from re_agent.orchestrator.single import reverse_single
from re_agent.search import MCP_URL, USER_AGENT, ParallelSearch

REFERENCE_URL = "https://en.cppreference.com/w/cpp/string/byte/memcpy?ref=api&lang=en"


class ReferenceLLM:
    supports_conversations = False

    def __init__(self, actions: list[str] | None = None) -> None:
        self.actions = actions if actions is not None else ["web_search", "web_fetch"]
        self.messages: list[list[Message]] = []

    def send(self, messages: list[Message], **kwargs: Any) -> str:
        self.messages.append(list(messages))
        index = len(self.messages) - 1
        if index < len(self.actions):
            tool = self.actions[index]
            target = REFERENCE_URL if tool == "web_fetch" else "C memcpy overlapping memory documentation"
            return json.dumps({"actions": [{"tool": tool, "target": target}]})
        return "```cpp\nvoid CTest::Foo() {}\n```\nREVERSED_FUNCTION: CTest::Foo (0x100)"

    def new_conversation(self, system: str) -> str:
        return ""

    def resume(self, conversation_id: str, message: str) -> str:
        raise AssertionError("Unexpected conversation resume")


class CheckerLLM(ReferenceLLM):
    def send(self, messages: list[Message], **kwargs: Any) -> str:
        return "VERDICT: PASS\nSUMMARY: Scripted checker result"


@pytest.fixture
def mcp_http(monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest) -> list[httpx.Request]:
    """Fake only the HTTP peer, preserving SDK handshakes and serialization."""
    requests: list[httpx.Request] = []
    real_client = httpx.AsyncClient
    scenario = getattr(request, "param", "success")

    async def respond(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        payload = json.loads(request.content)
        method = payload["method"]
        if method == "notifications/initialized":
            return httpx.Response(202)
        if method == "initialize":
            result: dict[str, Any] = {
                "protocolVersion": payload["params"]["protocolVersion"],
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "reference-fixture", "version": "1"},
            }
        elif method == "tools/list":
            result = {"tools": [{"name": tool, "inputSchema": {"type": "object"}}
                                for tool in ("web_search", "web_fetch")]}
        elif method == "tools/call":
            if scenario == "slow_call":
                await asyncio.sleep(10)
            if scenario == "read_timeout":
                raise httpx.ReadTimeout("read timed out", request=request)
            result = {"content": [{"type": "text", "text": (
                f"Source: {REFERENCE_URL}\nMemory regions must not overlap."
            )}]}
            if scenario == "tool_error":
                result = {"isError": True, "content": [{"type": "text", "text": "Test quota exhausted"}]}
            elif scenario == "empty":
                result = {"content": []}
            elif scenario == "long":
                result = {"content": [{"type": "text", "text": "x" * 20000}]}
        else:
            raise AssertionError(method)
        return httpx.Response(200, json={"jsonrpc": "2.0", "id": payload["id"], "result": result})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        assert kwargs["trust_env"] is True
        assert kwargs["follow_redirects"] is False
        return real_client(transport=httpx.MockTransport(respond), **kwargs)

    monkeypatch.setattr("re_agent.search.httpx.AsyncClient", client)
    return requests


def test_default_orchestrator_consumes_search_and_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mcp_http: list[httpx.Request],
) -> None:
    monkeypatch.chdir(tmp_path)
    # Ambient keys must not turn anonymous lookup into authenticated traffic.
    monkeypatch.setenv("PARALLEL_API_KEY", "must-not-be-sent")
    monkeypatch.setenv("OPENAI_API_KEY", "must-not-be-sent")
    config = load_config(cli_overrides={
        "validation.enabled": False, "parity.enabled": False,
        "orchestrator.objective_verifier_enabled": False,
    })
    llm = ReferenceLLM()
    result = reverse_single(FunctionTarget("0x100", "CTest", "Foo"), config, StubBackend(), llm,
                            checker_llm=CheckerLLM())
    assert result.success
    assert len(llm.messages) == 3
    assert "untrusted public reference" in llm.messages[1][-1].content
    assert REFERENCE_URL in llm.messages[1][-1].content
    assert "Memory regions must not overlap" in llm.messages[2][-1].content
    assert "web_search" in llm.messages[0][0].content
    payloads = [json.loads(r.content) for r in mcp_http]
    assert [p["method"] for p in payloads].count("initialize") == 2
    assert [p["method"] for p in payloads].count("tools/list") == 2
    calls = [p["params"] for p in payloads if p["method"] == "tools/call"]
    assert calls[0]["name"] == "web_search"
    assert calls[0]["arguments"]["search_queries"] == ["C memcpy overlapping memory documentation"]
    assert "public API or file-format documentation" in calls[0]["arguments"]["objective"]
    assert calls[1]["arguments"]["urls"] == [REFERENCE_URL]
    assert calls[1]["arguments"]["full_content"] is False
    assert "API behavior" in calls[1]["arguments"]["objective"]
    assert len(calls[0]["arguments"]["session_id"]) == 32
    assert calls[0]["arguments"]["session_id"] == calls[1]["arguments"]["session_id"]
    for request in mcp_http:
        assert str(request.url) == MCP_URL
        assert request.headers["user-agent"] == USER_AGENT
        assert "authorization" not in request.headers
        assert "must-not-be-sent" not in request.content.decode()


@pytest.mark.parametrize("settings", [{"search.enabled": False}, {"search.provider": "none"}])
def test_saved_disable_and_explicit_override(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mcp_http: list[httpx.Request], settings: dict[str, object],
) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("search:\n  enabled: false\n  provider: none\n  timeout_s: 9\n")
    config = load_config(path)
    assert config.search == SearchConfig(enabled=False, provider="none", timeout_s=9)
    enabled = load_config(path, cli_overrides={"search.enabled": True, "search.provider": "parallel"})
    assert enabled.search == SearchConfig(timeout_s=9)
    disabled = load_config(cli_overrides=settings)
    agent = ReverserAgent(ReferenceLLM(["web_search"]), StubBackend(), search_config=disabled.search)
    agent.reverse(FunctionTarget("0x100", "CTest", "Foo"))
    assert not mcp_http
    assert "Public-reference tools" not in agent._system_prompt()
    assert "unavailable" in agent._history[-2].content


@pytest.mark.parametrize("enabled,budget", [(False, 8), (True, 0)])
def test_investigation_permission_and_budget(
    mcp_http: list[httpx.Request], enabled: bool, budget: int,
) -> None:
    agent = ReverserAgent(ReferenceLLM(["web_search"]), StubBackend(),
                          investigation_enabled=enabled, max_investigations=budget)
    with pytest.raises((ValueError, RuntimeError), match="evidence request|budget exhausted"):
        agent.reverse(FunctionTarget("0x100", "CTest", "Foo"))
    assert not mcp_http
    assert "Public-reference tools" not in agent._system_prompt()


def test_web_actions_share_investigation_budget(mcp_http: list[httpx.Request]) -> None:
    llm = ReferenceLLM(["web_search", "web_fetch"])
    agent = ReverserAgent(llm, StubBackend(), max_investigations=1)
    with pytest.raises(RuntimeError, match="budget exhausted"):
        agent.reverse(FunctionTarget("0x100", "CTest", "Foo"))
    calls = [json.loads(r.content) for r in mcp_http if json.loads(r.content)["method"] == "tools/call"]
    assert len(calls) == 1


@pytest.mark.parametrize("url", ["file:///etc/passwd", "https://user:secret@example.com/", "https:///missing", ""])
def test_invalid_urls_never_reach_network(mcp_http: list[httpx.Request], url: str) -> None:
    assert "ERROR" in ParallelSearch(SearchConfig()).execute("web_fetch", url)
    assert not mcp_http


@pytest.mark.parametrize("overrides", [{"search.provider": "unknown"}, {"search.timeout_s": 0}])
def test_invalid_search_config_has_no_fallback(overrides: dict[str, object]) -> None:
    with pytest.raises(ValueError, match="search\\."):
        load_config(cli_overrides=overrides)


def test_transport_error_reaches_agent_without_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.AsyncClient
    requests: list[httpx.Request] = []

    def fail(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503, text="unavailable")

    monkeypatch.setattr("re_agent.search.httpx.AsyncClient",
                        lambda **kwargs: real_client(transport=httpx.MockTransport(fail), **kwargs))
    llm = ReferenceLLM(["web_search"])
    ReverserAgent(llm, StubBackend()).reverse(FunctionTarget("0x100", "CTest", "Foo"))
    assert "ERROR" in llm.messages[1][-1].content
    assert "503" in llm.messages[1][-1].content
    assert len(requests) == 1


def test_whole_operation_timeout_and_cancellation(monkeypatch: pytest.MonkeyPatch) -> None:
    finished: list[bool] = []

    async def slow(self: ParallelSearch, tool: str, arguments: dict[str, object]) -> str:
        try:
            await asyncio.sleep(10)
        finally:
            finished.append(True)
        return "unreachable"

    monkeypatch.setattr(ParallelSearch, "_call", slow)
    search = ParallelSearch(SearchConfig())
    search.config.timeout_s = 0.01  # type: ignore[assignment]
    llm = ReferenceLLM(["web_search"])
    ReverserAgent(llm, StubBackend(), search_config=search.config).reverse(FunctionTarget("0x100", "CTest", "Foo"))
    assert "timed out" in llm.messages[1][-1].content
    assert finished == [True]

    async def cancel(self: ParallelSearch, tool: str, arguments: dict[str, object]) -> str:
        raise asyncio.CancelledError()

    monkeypatch.setattr(ParallelSearch, "_call", cancel)
    with pytest.raises(asyncio.CancelledError):
        ReverserAgent(ReferenceLLM(["web_search"]), StubBackend()).reverse(FunctionTarget("0x100", "CTest", "Foo"))


@pytest.mark.parametrize("mcp_http,expected", [
    ("tool_error", "Test quota exhausted"), ("empty", "no text reference"), ("read_timeout", "read timed out"),
], indirect=["mcp_http"])
def test_mcp_failures_consumed_as_errors(mcp_http: list[httpx.Request], expected: str) -> None:
    llm = ReferenceLLM(["web_search"])
    ReverserAgent(llm, StubBackend()).reverse(FunctionTarget("0x100", "CTest", "Foo"))
    assert "ERROR" in llm.messages[1][-1].content
    assert expected in llm.messages[1][-1].content
    assert len([r for r in mcp_http if json.loads(r.content)["method"] == "tools/call"]) == 1


@pytest.mark.parametrize("mcp_http", ["long"], indirect=True)
def test_reference_text_is_bounded(mcp_http: list[httpx.Request]) -> None:
    llm = ReferenceLLM(["web_search"])
    ReverserAgent(llm, StubBackend()).reverse(FunctionTarget("0x100", "CTest", "Foo"))
    assert "x" * 12000 not in llm.messages[1][-1].content
    assert len(llm.messages[1][-1].content) < 12500


@pytest.mark.parametrize("mcp_http", ["slow_call"], indirect=True)
def test_actual_sdk_timeout_and_cancel_unwind(mcp_http: list[httpx.Request]) -> None:
    search = ParallelSearch(SearchConfig())
    search.config.timeout_s = 0.1  # type: ignore[assignment]
    assert "timed out" in search.execute("web_search", "public C documentation")
    assert any(json.loads(r.content)["method"] == "tools/call" for r in mcp_http)

    async def cancel() -> None:
        before = len(mcp_http)
        task = asyncio.create_task(search._bounded_call("web_search", search._arguments("web_search", "public C docs")))
        for _ in range(1000):
            if len(mcp_http) >= before + 4:
                break
            await asyncio.sleep(0.001)
        assert len(mcp_http) >= before + 4
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel())


def test_conversation_provider_consumes_web_result(mcp_http: list[httpx.Request]) -> None:
    class ConversationLLM(ReferenceLLM):
        supports_conversations = True

        def __init__(self) -> None:
            super().__init__(["web_search"])
            self.resumes: list[str] = []

        def new_conversation(self, system: str) -> str:
            assert "Public-reference tools" in system
            return "saved-conversation"

        def resume(self, conversation_id: str, message: str) -> str:
            assert conversation_id == "saved-conversation"
            self.resumes.append(message)
            return self.send([Message(role="user", content=message)])

    llm = ConversationLLM()
    code, _ = ReverserAgent(llm, StubBackend()).reverse(FunctionTarget("0x100", "CTest", "Foo"))
    assert "void CTest::Foo()" in code
    assert REFERENCE_URL in llm.resumes[1]
    assert "untrusted public reference" in llm.resumes[1]
