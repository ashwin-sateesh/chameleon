from __future__ import annotations

from pathlib import Path

from chameleon.agents.guardian import _parse_verdict
from chameleon.llm import parse_json_object
from chameleon.mcp_client import find_ref, normalize_tool_arguments

AGENTS = Path(__file__).resolve().parents[1] / "src" / "chameleon" / "agents"
FORBIDDEN = ("mcp_client", "from mcp", "import mcp")


def test_planner_and_guardian_have_no_mcp():
    for name in ("planner.py", "guardian.py"):
        text = (AGENTS / name).read_text()
        lowered = text.lower()
        for token in FORBIDDEN:
            assert token not in text, f"{name} must not reference {token}"
            assert token not in lowered, f"{name} must not reference {token}"


def test_guardian_parse_proceed_and_ask():
    proceed = _parse_verdict({"decision": "PROCEED", "reason": "snapshot only"})
    assert proceed.decision == "PROCEED"
    ask = _parse_verdict({"decision": "ASK", "question": "Which t-shirt?", "reason": "two matches"})
    assert ask.decision == "ASK"
    assert ask.question == "Which t-shirt?"
    wait = _parse_verdict({"decision": "WAIT", "reason": "user will click"})
    assert wait.decision == "WAIT"
    done = _parse_verdict({"decision": "DONE", "reason": "user finished"})
    assert done.decision == "DONE"


def test_parse_json_object_fenced():
    data = parse_json_object('Sure.\n```json\n{"tool": "browser_click", "arguments": {"target": "e1"}}\n```\n')
    assert data["tool"] == "browser_click"
    assert data["arguments"]["target"] == "e1"


def test_parse_json_object_extra_trailing_object():
    data = parse_json_object(
        '{"tool": "browser_snapshot", "arguments": {}, "reason": "see dropdown", "subgoal_complete": false}\n'
        '{"tool": "browser_click", "arguments": {"target": "e1"}}\n'
    )
    assert data["tool"] == "browser_snapshot"
    assert data["subgoal_complete"] is False


def test_normalize_ref_to_target():
    args = normalize_tool_arguments("browser_click", {"ref": "e12", "element": "Login"})
    assert args["target"] == "e12"
    assert "ref" not in args


def test_find_ref_from_snapshot():
    snap = '''
    - textbox "Username" [ref=e2]
    - textbox "Password" [ref=e3]
    - button "Login" [ref=e4]
    '''
    assert find_ref(snap, "Username") == "e2"
    assert find_ref(snap, "Login") == "e4"
