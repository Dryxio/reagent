"""Reverser agent — gathers context and asks LLM to produce reversed C++ code."""

from __future__ import annotations

import json
import re
from pathlib import Path

from re_agent.agents.source_context import SourceContextBuilder
from re_agent.backend.protocol import REBackend
from re_agent.config.schema import ProjectProfile
from re_agent.core.knowledge_graph import KnowledgeGraph
from re_agent.core.models import FunctionTarget
from re_agent.core.session import Session
from re_agent.llm.protocol import LLMProvider, Message
from re_agent.parity.source_indexer import SourceIndexer
from re_agent.utils.evidence import bounded_evidence
from re_agent.utils.templates import render_template

PROMPTS_DIR = Path(__file__).parent / "prompts"
CODE_BLOCK_RE = re.compile(r"```(?:cpp|c\+\+)?\s*\n(.*?)```", re.S)
REVERSED_TAG_RE = re.compile(r"REVERSED_FUNCTION:\s*(.+)")


class ReverserAgent:
    """Gathers decompile context and asks the LLM to reverse a function."""

    def __init__(
        self,
        llm: LLMProvider,
        backend: REBackend,
        source_root: Path | None = None,
        project_profile: ProjectProfile | None = None,
        indexer: SourceIndexer | None = None,
        session: Session | None = None,
        report_dir: Path | None = None,
        investigation_enabled: bool = True,
        max_investigations: int = 8,
    ) -> None:
        self.llm = llm
        self._session = session
        self.backend = backend
        self._project_profile = project_profile
        self._source_context_builder: SourceContextBuilder | None = None
        if source_root is not None and project_profile is not None and source_root.exists():
            self._source_context_builder = SourceContextBuilder(
                source_root=source_root,
                profile=project_profile,
                indexer=indexer,
                session=session,
                report_dir=report_dir,
            )
        self._conversation_id: str | None = None
        self._history: list[Message] = []
        self._investigation_enabled = investigation_enabled
        self._max_investigations = max(0, max_investigations)
        self._knowledge_graph = KnowledgeGraph(report_dir / "knowledge-graph.json") if report_dir is not None else None
        self.last_prompt: str = ""
        self.last_response: str = ""

    def reverse(self, target: FunctionTarget) -> tuple[str, str]:
        """Reverse a function. Returns (code, reversed_function_tag)."""
        # Gather context
        decompile_result = self.backend.decompile(target.address)
        decompiled = decompile_result.raw_output

        caps = self.backend.capabilities

        xrefs_text = ""
        if caps.has_xrefs:
            try:
                xrefs = self.backend.xrefs_from(target.address)
                xrefs_text = "\n".join(f"- {x.name} ({x.address}) [{x.ref_type}]" for x in xrefs) or "None found"
            except Exception:
                xrefs_text = "Unavailable"

        structs_text = ""
        if caps.has_structs and target.class_name:
            try:
                struct = self.backend.get_struct(target.class_name)
                if struct:
                    structs_text = f"{struct.name} (size: {struct.size})\n"
                    structs_text += "\n".join(
                        f"  +0x{f.offset:X} {f.type_str} {f.name} (size: {f.size})" for f in struct.fields
                    )
            except Exception:
                structs_text = "Unavailable"

        system_prompt = render_template(PROMPTS_DIR / "reverser_system.md")
        source_context = ""
        if self._source_context_builder is not None:
            source_context = self._source_context_builder.build(target)
        investigation_context = self._build_investigation_context(target)
        task_prompt = render_template(
            PROMPTS_DIR / "reverser_task.md",
            class_name=target.class_name,
            function_name=target.function_name,
            address=target.address,
            decompiled=decompiled,
            xrefs=xrefs_text or "None",
            structs=structs_text or "None",
            source_context=source_context or "None",
            investigation_context=investigation_context or "None",
            language_standard=(self._project_profile.language_standard if self._project_profile else "C++"),
            project_rules=self._project_rules(),
        )

        if self._session is not None:
            feedback = self._session.previous_feedback(target.address)
            if feedback:
                task_prompt += "\n\nPrevious attempt checkpoint (correct its failures):\n" + feedback
        if self._conversation_id is None and self.llm.supports_conversations:
            self._conversation_id = self.llm.new_conversation(system_prompt)

        self.last_prompt = task_prompt

        if self._conversation_id:
            response = self.llm.resume(self._conversation_id, task_prompt)
        else:
            messages = [
                Message(role="system", content=system_prompt),
                Message(role="user", content=task_prompt),
            ]
            response = self.llm.send(messages)
            self._history = [*messages, Message(role="assistant", content=response)]

        self.last_response = response
        response = self._run_action_loop(response, target, system_prompt, task_prompt)
        self.last_response = response
        code = self._extract_code(response)
        tag = self._extract_tag(response)
        return code, tag

    def _project_rules(self) -> str:
        if self._project_profile is None:
            return "- No additional project-specific rules"
        rules = self._project_profile.prompt_rules
        return "\n".join(f"- {rule}" for rule in rules) or "- No additional project-specific rules"

    def _build_investigation_context(self, target: FunctionTarget) -> str:
        """Collect bounded structured evidence exposed by the RE backend."""
        if not self._investigation_enabled or self._max_investigations == 0:
            return ""
        artifacts: list[str] = []

        def add(label: str, getter: object, argument: str) -> None:
            if len(artifacts) >= self._max_investigations or not callable(getter):
                return
            try:
                artifact = getter(argument)
            except Exception:
                return
            if artifact is None:
                return
            content = getattr(artifact, "content", "")
            if content:
                if label == "Function evidence bundle" and self._knowledge_graph is not None:
                    self._knowledge_graph.ingest_context(str(content))
                artifacts.append(f"## {label}\n{bounded_evidence(str(content), 8000)}")

        caps = self.backend.capabilities
        if getattr(caps, "has_context", False):
            add("Function evidence bundle", getattr(self.backend, "get_context", None), target.address)
        if getattr(caps, "has_vtables", False) and target.class_name:
            add("Vtable", getattr(self.backend, "get_vtable", None), target.class_name)
        if getattr(caps, "has_cfg", False):
            add("Control-flow graph", getattr(self.backend, "get_cfg", None), target.address)
        if getattr(caps, "has_pcode", False):
            add("Normalized high P-code", getattr(self.backend, "get_pcode", None), target.address)
        if self._knowledge_graph is not None:
            neighborhood = self._knowledge_graph.neighborhood(target.address)
            if neighborhood and neighborhood != '{\n  "nodes": {},\n  "edges": []\n}':
                artifacts.append(f"## Persistent knowledge graph neighborhood\n{neighborhood}")
        return "\n\n".join(artifacts)

    def _run_action_loop(
        self,
        response: str,
        target: FunctionTarget,
        system_prompt: str,
        task_prompt: str,
    ) -> str:
        """Execute bounded, read-only backend actions requested by the model."""
        if not self._investigation_enabled:
            return response
        history = self._history or [
            Message(role="system", content=system_prompt),
            Message(role="user", content=task_prompt),
            Message(role="assistant", content=response),
        ]
        used = 0
        while used < self._max_investigations:
            payload = self._extract_json(response)
            actions = payload.get("actions") if payload is not None else None
            if not isinstance(actions, list) or not actions:
                break
            results: list[str] = []
            for action in actions:
                if used >= self._max_investigations:
                    break
                if not isinstance(action, dict):
                    continue
                tool = str(action.get("tool", ""))
                argument = str(action.get("target") or target.address)
                results.append(self._execute_action(tool, argument))
                used += 1
            if not results:
                break
            tool_message = (
                "Read-only reverse-engineering tool results:\n\n"
                + "\n\n".join(results)
                + "\n\nNow return the final reversed function, or request more evidence within the remaining budget."
            )
            self.last_prompt = tool_message
            if self._conversation_id:
                response = self.llm.resume(self._conversation_id, tool_message)
            else:
                history.append(Message(role="user", content=tool_message))
                response = self.llm.send(history)
                history.append(Message(role="assistant", content=response))
                self._history = history
        payload = self._extract_json(response)
        if payload is not None and "actions" in payload:
            raise RuntimeError("Investigation budget exhausted before a code candidate was produced")
        return response

    def _execute_action(self, tool: str, argument: str) -> str:
        methods = {
            "decompile": "decompile",
            "xrefs_from": "xrefs_from",
            "xrefs_to": "xrefs_to",
            "struct": "get_struct",
            "enum": "get_enum",
            "vtable": "get_vtable",
            "global": "get_global",
            "strings": "search_strings",
            "context": "get_context",
            "pcode": "get_pcode",
            "cfg": "get_cfg",
        }
        method_name = methods.get(tool)
        method = getattr(self.backend, method_name, None) if method_name else None
        if not callable(method):
            return f"TOOL {tool}({argument}): unavailable"
        try:
            value = method(argument)
        except Exception as exc:
            return f"TOOL {tool}({argument}) ERROR: {exc}"
        if value is None:
            rendered = "not found"
        elif hasattr(value, "content"):
            rendered = str(value.content)
        elif hasattr(value, "raw_output"):
            rendered = str(value.raw_output)
        else:
            rendered = repr(value)
        return f"TOOL {tool}({argument}):\n{bounded_evidence(rendered, 12000)}"

    def fix(
        self,
        checker_report: str,
        issues: list[str],
        fix_instructions: list[str],
        target: FunctionTarget,
        objective_findings: list[str] | None = None,
    ) -> tuple[str, str]:
        """Ask the reverser to fix code based on checker feedback."""
        all_issues = list(issues)
        all_fix_instructions = list(fix_instructions)
        if objective_findings:
            all_issues.extend(f"objective verifier: {finding}" for finding in objective_findings)
            all_fix_instructions.extend("Resolve objective mismatch: " + finding for finding in objective_findings)
        fix_prompt = render_template(
            PROMPTS_DIR / "fix_instructions.md",
            checker_report=checker_report,
            issues="\n".join(f"- {i}" for i in all_issues),
            fix_instructions="\n".join(f"- {i}" for i in all_fix_instructions),
            class_name=target.class_name,
            function_name=target.function_name,
            address=target.address,
        )

        self.last_prompt = fix_prompt

        if self._conversation_id:
            response = self.llm.resume(self._conversation_id, fix_prompt)
        else:
            self._history.append(Message(role="user", content=fix_prompt))
            response = self.llm.send(self._history)
            self._history.append(Message(role="assistant", content=response))

        self.last_response = response
        system_prompt = render_template(PROMPTS_DIR / "reverser_system.md")
        response = self._run_action_loop(response, target, system_prompt, fix_prompt)
        self.last_response = response
        code = self._extract_code(response)
        tag = self._extract_tag(response)
        return code, tag

    @staticmethod
    def _extract_code(response: str) -> str:
        payload = ReverserAgent._extract_json(response)
        if payload is not None and ("actions" in payload or "blocked" in payload):
            raise ValueError("Expected a code candidate, received an unresolved evidence request")
        if payload is not None and isinstance(payload.get("code"), str):
            return str(payload["code"]).strip()
        m = CODE_BLOCK_RE.search(response)
        return m.group(1).strip() if m else response.strip()

    @staticmethod
    def _extract_tag(response: str) -> str:
        payload = ReverserAgent._extract_json(response)
        if payload is not None and isinstance(payload.get("reversed_function"), str):
            return str(payload["reversed_function"]).strip()
        m = REVERSED_TAG_RE.search(response)
        return m.group(1).strip() if m else ""

    @staticmethod
    def _extract_json(response: str) -> dict[str, object] | None:
        text = response.strip()
        if text.startswith("```json") and text.endswith("```"):
            text = text[7:-3].strip()
        if not text.startswith("{"):
            return None
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return None
        return payload if isinstance(payload, dict) else None
