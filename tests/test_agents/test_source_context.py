"""Tests for source-context retrieval in the reverser."""
from __future__ import annotations

from pathlib import Path

from re_agent.agents.reverser import ReverserAgent
from re_agent.config.schema import ProjectProfile
from re_agent.core.models import FunctionTarget, ReversalResult
from re_agent.core.session import Session
from re_agent.llm.protocol import Message


class RecordingLLM:
    def __init__(self, response: str) -> None:
        self.response = response
        self.messages: list[Message] = []

    def send(self, messages: list[Message], **kwargs: object) -> str:
        self.messages = messages
        return self.response

    @property
    def supports_conversations(self) -> bool:
        return False

    def new_conversation(self, system: str) -> str:
        return ""

    def resume(self, conversation_id: str, message: str) -> str:
        return ""


class StubBackendForPrompt:
    @property
    def capabilities(self) -> object:
        class _Caps:
            has_xrefs = False
            has_structs = False
        return _Caps()

    def decompile(self, target: str) -> object:
        class _Dec:
            raw_output = "void CTrain::ProcessControl() { UpdateTrainNodes(); }"
        return _Dec()


def test_reverser_prompt_includes_source_context(tmp_path: Path) -> None:
    (tmp_path / "CTrain.h").write_text(
        """\
class CTrain {
public:
    void ProcessControl();
    void Shutdown();
    float m_fSpeed;
};
""",
        encoding="utf-8",
    )
    (tmp_path / "CTrain.cpp").write_text(
        """\
void CTrain::Shutdown() {
    plugin::CallMethod<0x6F5900, CTrain*>(this);
}

void CTrain::UpdateTrainNodes() {
    m_fSpeed += 1.0f;
}
""",
        encoding="utf-8",
    )

    session = Session(tmp_path / "progress.json")
    report_dir = tmp_path / "reports"
    code_dir = report_dir / "code"
    code_dir.mkdir(parents=True)
    generated_path = code_dir / "0x6F5900_CTrain_Shutdown.cpp"
    generated_path.write_text("void CTrain::Shutdown() { plugin::CallMethod<0x6F5900, CTrain*>(this); }\n")
    session.record_result(ReversalResult(
        target=FunctionTarget(address="0x6F5900", class_name="CTrain", function_name="Shutdown"),
        code="void CTrain::Shutdown() { plugin::CallMethod<0x6F5900, CTrain*>(this); }",
        rounds_used=1,
        success=True,
    ))

    llm = RecordingLLM(
        "```cpp\nvoid CTrain::ProcessControl() { UpdateTrainNodes(); }\n```\n"
        "REVERSED_FUNCTION: CTrain::ProcessControl (0x6F86A0)"
    )
    profile = ProjectProfile(source_root=str(tmp_path), source_extensions=[".cpp", ".h", ".hpp"], hooks_csv=None)
    reverser = ReverserAgent(
        llm,
        StubBackendForPrompt(),
        source_root=tmp_path,
        project_profile=profile,
        session=session,
        report_dir=report_dir,
    )

    reverser.reverse(FunctionTarget(address="0x6F86A0", class_name="CTrain", function_name="ProcessControl"))

    assert "Class header:" in reverser.last_prompt
    assert "Sibling methods:" in reverser.last_prompt
    assert "Recent accepted reversals (see validation status):" in reverser.last_prompt
    assert "m_fSpeed" in reverser.last_prompt
    assert "Shutdown" in reverser.last_prompt
