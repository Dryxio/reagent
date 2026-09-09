"""Keep reconstruction/review instructions consistent with missing type evidence."""
from re_agent.agents.checker import PROMPTS_DIR
from re_agent.utils.templates import render_template


def test_missing_layout_does_not_require_invented_members():
    reverser = render_template(PROMPTS_DIR / "reverser_task.md")
    checker = render_template(PROMPTS_DIR / "checker_system.md")
    assert "preserve the exact byte offset and access width" in reverser
    assert "do not invent structs or member names" in reverser
    assert "accept explicit byte offsets" in checker
    assert "do not fail code solely for lacking member names" in checker


def test_requested_candidate_matches_single_body_overlay_contract():
    prompt = render_template(PROMPTS_DIR / "reverser_task.md")
    assert "exactly one complete function implementation" in prompt
    assert "report missing declarations outside the code block" in prompt
