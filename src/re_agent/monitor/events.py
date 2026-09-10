"""Bounded, incremental views of native agent JSONL event logs."""
from __future__ import annotations

import json
import re
from collections import OrderedDict
from pathlib import Path
from typing import Any


class AgentEvents:
    def __init__(self) -> None:
        self.path: Path | None = None
        self.offset = 0
        self.pending = b""
        self.agents: OrderedDict[str, dict[str, Any]] = OrderedDict()

    def agent(self, key: str, label: str = "") -> dict[str, Any]:
        if key not in self.agents:
            self.agents[key] = {"id": key, "label": label or key, "status": "running", "text": "", "log": ""}
        self.agents.move_to_end(key)
        while len(self.agents) > 128:
            self.agents.popitem(last=False)
        return self.agents[key]

    def read(self, path: Path) -> list[dict[str, Any]]:
        if path != self.path or path.stat().st_size < self.offset:
            self.path, self.offset, self.pending = path, 0, b""
        with path.open("rb") as stream:
            stream.seek(self.offset)
            data = stream.read(1024 * 1024)
            self.offset = stream.tell()
        lines = (self.pending + data).split(b"\n")
        self.pending = lines.pop()[-1024 * 1024:]
        for line in lines:
            try:
                row = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(row, dict):
                self.consume(row, path.parent.name)
        return list(self.agents.values())

    def consume(self, row: dict[str, Any], batch: str) -> None:
        # Unattributed token events must never be guessed to belong to a child.
        shared = self.agent(f"{batch}:shared", f"{batch} · shared stream (unattributed)")
        kind = row.get("type")
        if kind == "text" and isinstance(row.get("data"), str):
            shared["text"] = (shared["text"] + row["data"])[-65536:]
        elif kind == "tool_call":
            shared["log"] = (shared["log"] + "\n" + json.dumps({
                "tool": row.get("toolName"), "input": row.get("rawInput")}, ensure_ascii=False))[-65536:]
        elif kind == "tool_call_update":
            raw = row.get("rawOutput")
            if not isinstance(raw, dict):
                return
            text = raw.get("text", "")
            match = re.search(r"subagent_id: ([\w-]+)", text) if isinstance(text, str) else None
            if match and "Subagent started in background" in text:
                label = re.search(r"description: ([^\n]+)", text)
                child = self.agent(match[1], f"{batch} · {label[1] if label else match[1]}")
                child["log"] = text[-65536:]
            multi = raw.get("MultiResult")
            results = multi.get("results", []) if isinstance(multi, dict) else []
            for result in results if isinstance(results, list) else []:
                if not isinstance(result, dict) or not isinstance(result.get("task_id"), str):
                    continue
                child = self.agent(result["task_id"], f"{batch} · {result.get('command', result['task_id'])}")
                child.update(status=result.get("status", "unknown"),
                             text=str(result.get("output", ""))[-65536:],
                             log=json.dumps({k: v for k, v in result.items() if k != "output"}, indent=2)[-65536:])
            if row.get("status") == "failed":
                shared["log"] = (shared["log"] + "\nERROR " + json.dumps(raw))[-65536:]
