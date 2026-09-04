"""Bound evidence without emitting broken structured JSON."""

from __future__ import annotations

import json
from typing import Any


def bounded_evidence(content: str, max_chars: int = 8000) -> str:
    if len(content) <= max_chars:
        return content
    try:
        value = json.loads(content)
    except ValueError:
        return content[: max_chars - 40] + "\n[truncated; request specific evidence]"
    limit = 32
    while limit:

        def prune(node: Any, depth: int = 0, limit: int = limit) -> Any:
            if depth > 8:
                return "[depth limited]"
            if isinstance(node, dict):
                return {key: prune(val, depth + 1) for key, val in list(node.items())[:limit]}
            if isinstance(node, list):
                return [prune(item, depth + 1) for item in node[:limit]]
            if isinstance(node, str):
                return node[: limit * 32]
            return node

        rendered = json.dumps({"truncated": True, "data": prune(value)}, ensure_ascii=True)
        if len(rendered) <= max_chars:
            return rendered
        limit //= 2
    return '{"truncated": true, "data": null}'
