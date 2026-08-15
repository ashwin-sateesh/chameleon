"""Guardian: PROCEED or ASK. No MCP. Invoked only on risk-tagged sub-goals."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel

from chameleon.llm import complete, parse_json_object
from chameleon.narration import say
from chameleon.profiles import ChecklistItem
from chameleon.state import GuardianAnswer

SYSTEM = """You are the Guardian. You decide whether a proposed browser action is safe
to run without the user, or whether the system must pause and ask a specific question.

You have NO browser tools. You only return JSON:
{
  "decision": "PROCEED" | "ASK",
  "question": "one concrete question if ASK, else null",
  "reason": "short why"
}

Rules:
- ASK on genuine ambiguity (multiple matching items, unclear choice).
- ASK when user-specific info is required and is not already in guardian_answers
  (name, email, phone, address, zip, resume path, cover letter text, etc.).
- ASK before irreversible actions (place order, submit application, pay, delete).
- If the user already answered this, PROCEED and apply their answer.
- Do not ASK vague questions. Ask one specific question.
- If the proposed action is still gathering page info (snapshot/navigate) and does
  not commit anything, PROCEED.
"""


class GuardianVerdict(BaseModel):
    decision: Literal["PROCEED", "ASK"]
    question: str | None = None
    reason: str = ""


def _parse_verdict(data: dict[str, Any]) -> GuardianVerdict:
    decision = str(data.get("decision") or "").strip().upper()
    if decision not in {"PROCEED", "ASK"}:
        text = str(data)
        if "ASK" in text.upper():
            decision = "ASK"
        else:
            decision = "PROCEED"
    question = data.get("question")
    if question is not None:
        question = str(question).strip() or None
    if decision == "ASK" and not question:
        question = "How should I proceed with this step?"
    return GuardianVerdict(
        decision=decision,  # type: ignore[arg-type]
        question=question,
        reason=str(data.get("reason") or ""),
    )


def guardian_check(
    *,
    proposed_action: dict[str, Any],
    subgoal: ChecklistItem,
    guardian_answers: list[GuardianAnswer],
    task: str,
) -> GuardianVerdict:
    answers = "\n".join(
        f"- Q: {a.question} A: {a.answer}" for a in guardian_answers
    ) or "(none yet)"
    prompt = f"""User task: {task}
Current sub-goal: {subgoal.goal}
Risk reason / tag: {subgoal.risk}

Proposed action:
{proposed_action}

Existing guardian answers:
{answers}

Decide PROCEED or ASK.
"""
    raw = complete(prompt, system=SYSTEM)
    verdict = _parse_verdict(parse_json_object(raw))
    say("guardian", f"{verdict.decision}: {verdict.reason or verdict.question or 'ok'}")
    return verdict
