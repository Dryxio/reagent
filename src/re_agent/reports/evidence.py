"""Export stored manifest evidence without backend queries or model calls."""
from __future__ import annotations

import json
from dataclasses import asdict
from pathlib import Path
from typing import Any

from re_agent.core.target_plan import TargetPlan


def _tsv(value: object) -> str:
    return str(value).replace("\\", "\\\\").replace("\t", "\\t").replace("\r", "\\r").replace("\n", "\\n")


def _write_rows(path: Path, rows: list[list[object]]) -> None:
    path.write_text("".join("\t".join(_tsv(value) for value in row) + "\n" for row in rows), encoding="utf-8")


def export_evidence(plan: TargetPlan, output: Path) -> None:
    """Require an empty destination so exports cannot overwrite unrelated files."""
    if output.exists() and (not output.is_dir() or any(output.iterdir())):
        raise ValueError("Evidence output must be a new or empty directory")
    output.mkdir(parents=True, exist_ok=True)
    packets = output / "functions"
    packets.mkdir()
    plan.save(output / "manifest.json")
    functions: list[list[object]] = [["address", "class", "function", "selection", "packet"]]
    calls: list[list[object]] = [["source", "target", "selected_target"]]
    references: list[list[object]] = [["function", "kind", "record_json", "origin"]]
    gaps: list[list[object]] = [["function", "site", "kind", "reason", "origin"]]
    index = ["# Stored function evidence", "", f"Input fingerprint: `{plan.identity}`", "",
             "Snapshot of returned evidence; missing or truncated records are not complete analysis.",
             "See [manifest](manifest.json), [functions](functions.tsv), [calls](calls.tsv),",
             "[references](references.tsv), and [gaps](gaps.tsv).", ""]
    addresses = {target.address for target in plan.functions}
    for target in sorted(plan.functions, key=lambda item: item.address):
        record = plan.evidence.get(target.address, {})
        packet: dict[str, Any] = {
            "schema_version": 1, "identity": plan.identity, "function": asdict(target),
            "stored_evidence": record,
            "evidence_available": bool(record),
            "gaps": [asdict(gap) for gap in plan.gaps if gap.function == target.address],
        }
        raw = json.dumps(packet, indent=2, sort_keys=True, ensure_ascii=True)
        packet_path = f"functions/{target.address}.json"
        (output / packet_path).write_text(raw + "\n", encoding="utf-8")
        functions.append([target.address, target.class_name, target.function_name,
                          record.get("selection", ""), packet_path])
        index.append(f"- [{target.address}]({packet_path})")
        context = record.get("context", {})
        if isinstance(context, dict):
            for kind in ("globals", "strings"):
                items = context.get(kind, [])
                if isinstance(items, list):
                    for item in items:
                        references.append([target.address, kind, json.dumps(item, sort_keys=True),
                                           context.get("origin", "unspecified")])
    for edge in sorted(plan.edges, key=lambda item: (item["source"], item["target"])):
        calls.append([edge["source"], edge["target"], edge["target"] in addresses])
    for gap in sorted(plan.gaps, key=lambda item: (
        item.function, item.site or "", item.kind, item.reason, item.origin
    )):
        gaps.append([gap.function, gap.site or "", gap.kind, gap.reason, gap.origin])
    for name, rows in (("functions", functions), ("calls", calls), ("references", references), ("gaps", gaps)):
        _write_rows(output / f"{name}.tsv", rows)
    (output / "INDEX.md").write_text("\n".join(index) + "\n", encoding="utf-8")
