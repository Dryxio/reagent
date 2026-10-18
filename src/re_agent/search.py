"""Anonymous public-reference lookup through Parallel's Streamable HTTP MCP."""

from __future__ import annotations

import asyncio
import uuid
from urllib.parse import urlsplit

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import TextContent

from re_agent import __version__
from re_agent.config.schema import SearchConfig
from re_agent.utils.evidence import bounded_evidence

MCP_URL = "https://search.parallel.ai/mcp"
USER_AGENT = f"auto-re-agent/{__version__} (+https://github.com/Dryxio/reagent)"


def _error_text(error: BaseException) -> str:
    """Keep useful transport errors when an async task group wraps them."""
    children = getattr(error, "exceptions", ())
    if children:
        return "; ".join(_error_text(child) for child in children)
    return str(error) or type(error).__name__


class ParallelSearch:
    """One logical search conversation per reverser; never reads ambient API keys."""

    def __init__(self, config: SearchConfig) -> None:
        self.config = config
        self.session_id = uuid.uuid4().hex

    @property
    def enabled(self) -> bool:
        return self.config.enabled and self.config.provider == "parallel"

    def execute(self, tool: str, argument: str) -> str:
        prefix = f"TOOL {tool}({argument})"
        if not self.enabled:
            return f"{prefix}: unavailable (web lookup disabled)"
        try:
            arguments = self._arguments(tool, argument)
            text = asyncio.run(self._bounded_call(tool, arguments))
        except asyncio.TimeoutError:
            return f"{prefix} ERROR: web lookup timed out after {self.config.timeout_s}s"
        except Exception as exc:
            # Do not catch KeyboardInterrupt/SystemExit or cancellation.
            return f"{prefix} ERROR: {bounded_evidence(_error_text(exc), 2000)}"
        return f"{prefix} (untrusted public reference):\n{bounded_evidence(text, 12000)}"

    def _arguments(self, tool: str, argument: str) -> dict[str, object]:
        if not argument.strip() or len(argument) > 2000:
            raise ValueError("web lookup target must contain 1-2000 characters")
        if tool == "web_search":
            return {
                "objective": f"Find public API or file-format documentation for: {argument}",
                "search_queries": [argument],
                "session_id": self.session_id,
            }
        if tool == "web_fetch":
            url = urlsplit(argument)
            if url.scheme not in {"http", "https"} or not url.hostname or url.username or url.password:
                raise ValueError("web_fetch requires an HTTP(S) URL without credentials")
            return {
                "urls": [argument],
                "objective": (
                    "Extract API behavior, preconditions, return values and examples from this technical reference."
                ),
                "full_content": False,
                "session_id": self.session_id,
            }
        raise ValueError("Unsupported web lookup tool")

    async def _bounded_call(self, tool: str, arguments: dict[str, object]) -> str:
        # Bound the entire initialize/discovery/call/cleanup operation, not only
        # an individual HTTP read. Cancellation unwinds both SDK contexts.
        return await asyncio.wait_for(self._call(tool, arguments), timeout=self.config.timeout_s)

    async def _call(self, tool: str, arguments: dict[str, object]) -> str:
        async with (
            httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=self.config.timeout_s,
                follow_redirects=False,
                trust_env=True,
            ) as client,
            streamable_http_client(MCP_URL, http_client=client) as (read, write, _),
            ClientSession(read, write) as session,
        ):
            await session.initialize()
            tools = await session.list_tools()
            if tool not in {item.name for item in tools.tools}:
                raise ValueError(f"Parallel MCP does not advertise {tool}")
            result = await session.call_tool(tool, arguments)
            text = "\n".join(item.text for item in result.content if isinstance(item, TextContent))
            if result.isError:
                raise RuntimeError(text or "Parallel MCP tool failed")
            if not text.strip():
                raise ValueError("Parallel MCP returned no text reference")
            return text
