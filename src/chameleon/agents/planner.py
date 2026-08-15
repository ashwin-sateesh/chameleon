"""Planner: execute checklists from YAML; copilot observe via LLM. No MCP."""

from __future__ import annotations

from pydantic import BaseModel, Field

from chameleon.llm import complete, parse_json_object
from chameleon.narration import say
from chameleon.profiles import ChecklistItem, SiteProfile
from chameleon.state import GuardianAnswer, ObservedEvent

SNAPSHOT_CHARS = 12000

OBSERVE_SYSTEM = """You are the Planner in a copilot browser assistant.
The user drives the website. You only interpret what they are looking at
and suggest what they might do next. You never touch the browser.

Return ONLY JSON:
{
  "user_state": "short description of what the user is looking at",
  "possible_intents": ["...", "..."],
  "question": "one specific question or null",
  "should_ask": true,
  "suggested_micro_goal": null,
  "blocker": null
}

Rules:
- should_ask false on a generic homepage with no search/place yet — the idle prompt already told them to explore.
- should_ask false if nothing meaningful changed vs last_user_state.
- should_ask false if last_question is still unanswered.
- should_ask true when the user has landed on a city, place, search results, listing, or directions view and we have not asked about this state yet.
- Offer 3-5 concrete options that make sense NOW. Prefer intent_hints when they still apply, but adapt to the page.
- Do not suggest clicking the map canvas. Search box, sidebar, place cards, filters, directions panel only.
- If a cookie wall or CAPTCHA is clearly visible, set blocker to "cookie" or "captcha", should_ask true, and tell the user to handle it in the browser.
- Keep question to one sentence.
- suggested_micro_goal stays null unless the user already chose an intent this turn (they usually have not).
"""


class ObserveResult(BaseModel):
    user_state: str = ""
    possible_intents: list[str] = Field(default_factory=list)
    question: str | None = None
    should_ask: bool = False
    suggested_micro_goal: str | None = None
    blocker: str | None = None


def planner_plan(task: str, site_profile: SiteProfile) -> list[ChecklistItem]:
    checklist = [item.model_copy() for item in site_profile.checklist_template]
    say("planner", f"Task: {task}")
    say("planner", f"Site: {site_profile.name} ({site_profile.id})")
    for index, item in enumerate(checklist, start=1):
        say("planner", f"{index}. [{item.risk}] {item.goal}")
    return checklist


def planner_observe(
    *,
    snapshot: str,
    url: str | None,
    last_user_state: str | None,
    pending_question: str | None,
    guardian_answers: list[GuardianAnswer],
    observed_events: list[ObservedEvent],
    profile: SiteProfile,
    task: str,
) -> ObserveResult:
    hints = "\n".join(f"- {h}" for h in profile.intent_hints) or "(none)"
    answers = "\n".join(f"- Q: {a.question} A: {a.answer}" for a in guardian_answers[-8:]) or "(none yet)"
    recent = "\n".join(
        f"- {e.summary} ({e.url})" for e in observed_events[-6:]
    ) or "(none)"
    prompt = f"""User task: {task}
Site: {profile.name} base_url={profile.base_url}
Intent hints (not a script):
{hints}

Last interpreted user_state: {last_user_state or "(none)"}
Pending unanswered question: {pending_question or "(none)"}
Recent observed events:
{recent}
Guardian answers so far:
{answers}

Current URL: {url or "(unknown)"}
Current accessibility snapshot (truncated):
{(snapshot or "")[:SNAPSHOT_CHARS]}

Interpret the user's current state.
"""
    raw = complete(prompt, system=OBSERVE_SYSTEM)
    data = parse_json_object(raw)
    question = data.get("question")
    if question is not None:
        question = str(question).strip() or None
    goal = data.get("suggested_micro_goal")
    if goal is not None:
        goal = str(goal).strip() or None
    blocker = data.get("blocker")
    if blocker is not None:
        blocker = str(blocker).strip().lower() or None
        if blocker not in {"cookie", "captcha"}:
            blocker = None
    result = ObserveResult(
        user_state=str(data.get("user_state") or "").strip(),
        possible_intents=[str(x).strip() for x in (data.get("possible_intents") or []) if str(x).strip()],
        question=question,
        should_ask=bool(data.get("should_ask")) and bool(question),
        suggested_micro_goal=goal,
        blocker=blocker,
    )
    if pending_question:
        result.should_ask = False
    say("planner", result.user_state or "observing")
    if result.should_ask:
        say("planner", f"Ask: {result.question}")
    return result
