"""Guardian: PROCEED or ASK. No MCP. Invoked only on risk-tagged sub-goals."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from chameleon.ask_options import extract_ask_options
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
  "options": ["short clickable reply", "..."],
  "reason": "short why",
  "micro_goal": null
}

Rules:
- ASK on genuine ambiguity (multiple matching items, unclear choice).
- ASK when user-specific info is required and is not already in guardian_answers
  (name, email, phone, address, zip, resume path, cover letter text, etc.).
- If the Navigator proposed no tool because that info is missing, ASK. Put every
  missing field in one question (e.g. first name, last name, email, and phone).
- ASK before irreversible actions (place order, submit application, pay, delete).
- If the user already answered this, PROCEED and apply their answer.
- Do not ASK vague questions. Ask one specific question.
- If the user must pick among discrete choices (which item, yes/no to submit), put
  those short replies in options (2–6 items). Name the actual choices.
- If they must type free-form info (zip, email, phone, name), omit options or use [].
- If the proposed action is still gathering page info (snapshot/navigate) and does
  not commit anything, PROCEED.
"""


class GuardianVerdict(BaseModel):
    decision: Literal["PROCEED", "ASK", "WAIT", "DONE"]
    question: str | None = None
    reason: str = ""
    options: list[str] = Field(default_factory=list)
    micro_goal: str | None = None


def _parse_verdict(data: dict[str, Any]) -> GuardianVerdict:
    decision = str(data.get("decision") or "").strip().upper()
    if decision not in {"PROCEED", "ASK", "WAIT", "DONE"}:
        text = str(data).upper()
        if "DONE" in text:
            decision = "DONE"
        elif "WAIT" in text:
            decision = "WAIT"
        elif "ASK" in text:
            decision = "ASK"
        else:
            decision = "PROCEED"
    question = data.get("question")
    if question is not None:
        question = str(question).strip() or None
    if decision == "ASK" and not question:
        question = "How should I proceed with this step?"
    raw_opts = data.get("options") or []
    if isinstance(raw_opts, str):
        raw_opts = [raw_opts]
    extra = [str(item) for item in raw_opts if str(item).strip()]
    options = extract_ask_options(question or "", extra) if decision == "ASK" else []
    goal = data.get("micro_goal")
    if goal is not None:
        goal = str(goal).strip() or None
    return GuardianVerdict(
        decision=decision,  # type: ignore[arg-type]
        question=question,
        reason=str(data.get("reason") or ""),
        options=options,
        micro_goal=goal,
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


COPILOT_INTERPRET_SYSTEM = """You are the Guardian in a copilot browser assistant.
The user drives the site unless they explicitly consent to you acting.

You have NO browser tools. Return ONLY JSON:
{
  "decision": "PROCEED" | "ASK" | "WAIT" | "DONE",
  "question": "one concrete question if ASK, else null",
  "reason": "short why",
  "micro_goal": "concrete browser goal if PROCEED or if ASK is a consent-to-act question"
}

Decisions:
- DONE: user wants to finish (done, quit, stop, that's all, no thanks I'm finished).
- WAIT: user will keep exploring themselves ("I'll do it", "keep exploring", "not yet", "just looking").
- ASK: you need a consent question before acting. Choosing an intent (food, tourist, lodging) is NOT consent to act. Ask: "I'll <specific action>. Should I do that, or will you?"
- PROCEED: user clearly consented to you operating the page ("do it", "yes, search for me", "go ahead").

Rules:
- Picking "food" / "tourist spots" / "lodging" → ASK with a specific micro_goal, not PROCEED.
- "I'll do it" / "I'll click" → WAIT, clear any pending act.
- Do not invent irreversible actions (book, pay, share, submit) as the micro_goal.
- micro_goal must be one short burst: e.g. "search restaurants in San Francisco and show the list".
- If the message is a direct instruction ("find pizza") still ASK for consent unless they also said to do it now.
"""


COPILOT_ACTION_SYSTEM = """You are the Guardian reviewing one copilot Navigator action.
The user already consented to the micro-goal. Return ONLY JSON:
{
  "decision": "PROCEED" | "ASK" | "WAIT",
  "question": "one concrete question if ASK, else null",
  "reason": "short why",
  "micro_goal": null
}

PROCEED for snapshot, typing in search, clicking sidebar/place cards/filters/directions that complete the consented goal.
ASK if the action would leave the site, share, download, pay, book, submit, or delete.
WAIT if the action is clicking the map canvas — do not allow canvas clicks.
"""


def guardian_copilot_interpret(
    *,
    pending_question: str | None,
    user_reply: str,
    user_state: str | None,
    possible_intents: list[str],
    suggested_micro_goal: str | None,
    guardian_answers: list[GuardianAnswer],
    task: str,
    site_name: str,
) -> GuardianVerdict:
    answers = "\n".join(f"- Q: {a.question} A: {a.answer}" for a in guardian_answers[-8:]) or "(none yet)"
    intents = ", ".join(possible_intents) or "(none)"
    prompt = f"""User task: {task}
Site: {site_name}
Current user_state: {user_state or "(unknown)"}
Possible intents: {intents}
Suggested micro_goal from planner: {suggested_micro_goal or "(none)"}
Question the user was answering (if any): {pending_question or "(none — this is a freeform instruction)"}
User message: {user_reply}

Existing answers:
{answers}

Decide DONE, WAIT, ASK (consent), or PROCEED (act now).
"""
    raw = complete(prompt, system=COPILOT_INTERPRET_SYSTEM)
    verdict = _parse_verdict(parse_json_object(raw))
    if verdict.decision == "PROCEED" and not verdict.micro_goal:
        verdict.micro_goal = suggested_micro_goal
    if verdict.decision == "ASK" and not verdict.micro_goal:
        verdict.micro_goal = suggested_micro_goal
    say("guardian", f"{verdict.decision}: {verdict.reason or verdict.question or 'ok'}")
    return verdict


def guardian_copilot_action(
    *,
    proposed_action: dict[str, Any],
    micro_goal: str,
    task: str,
) -> GuardianVerdict:
    prompt = f"""User task: {task}
Consented micro-goal: {micro_goal}
Proposed action:
{proposed_action}

Decide PROCEED, ASK, or WAIT.
"""
    raw = complete(prompt, system=COPILOT_ACTION_SYSTEM)
    verdict = _parse_verdict(parse_json_object(raw))
    if verdict.decision == "DONE":
        verdict.decision = "ASK"
        verdict.question = verdict.question or "This action looks irreversible. Should I continue?"
    say("guardian", f"{verdict.decision}: {verdict.reason or verdict.question or 'ok'}")
    return verdict
