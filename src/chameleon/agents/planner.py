"""Planner: execute checklists from YAML; copilot observe via LLM. No MCP."""

from __future__ import annotations

from pydantic import BaseModel, Field

from chameleon.llm import complete, parse_json_object
from chameleon.narration import say
from chameleon.profiles import ChecklistItem, SiteProfile
from chameleon.state import GuardianAnswer, ObservedEvent

SNAPSHOT_CHARS = 12000

OBSERVE_SYSTEM = """You are the Planner in a terminal-led copilot. Same rules on every
copilot site (Maps, OSM, Airbnb, …). You never touch the browser.

After the agent acts OR the user clicks the UI: name THIS page in ~8 words.
Then a simple follow-up — never a multi-step plan.

Return ONLY JSON:
{
  "user_state": "eight words or fewer",
  "unmet_constraints": ["task bits not on this page, else []"],
  "possible_intents": ["at most 2 unused controls that ARE visible"],
  "question": "one short sentence or null",
  "should_ask": true,
  "suggested_micro_goal": "one concrete click/type if they pick an extra, else null",
  "blocker": null
}

unmet_constraints: parts of the user task (stars, Guest favorite, Open now, price,
a chip, a layer) that are NOT visibly applied and whose control is NOT in this
snapshot. List each once. Do not invent a control. Do not keep hunting.

possible_intents: only controls you can see on THIS snapshot. One extra = one
control. No nested menus, no "open Filters then …".

Question (one sentence):
- If unmet: "{constraint} isn't on this page. {0-2 visible extras}. Skip it / which / done?"
- Else if extras: "I can {extra1} or {extra2}. Which / done?"
- Else: should_ask false, question null.

Rules:
- Try-once: if the agent already searched/applied the core query, missing extras
  are unmet — tell the user. Never ask the agent to hunt again.
- Do NOT re-ask dates, guests, city, query, or filters already applied.
- Homepage with no search yet: should_ask false.
- Search form filled, results not showing: should_ask true — suggest Search.
- Listing/place: only listing extras (reviews, similar, directions). ASK before contact/book.
- Do not suggest clicking a map canvas.
- Cookie/CAPTCHA visible → blocker cookie|captcha, should_ask true.
- Keep it short. No essays.
"""


class ObserveResult(BaseModel):
    user_state: str = ""
    unmet_constraints: list[str] = Field(default_factory=list)
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
    replace_question: bool = False,
) -> ObserveResult:
    hints = "\n".join(f"- {h}" for h in profile.intent_hints) or "(none)"
    answers = "\n".join(f"- Q: {a.question} A: {a.answer}" for a in guardian_answers[-8:]) or "(none yet)"
    recent = "\n".join(
        f"- {e.summary} ({e.url})" for e in observed_events[-6:]
    ) or "(none)"
    prompt = f"""User task: {task}
Site: {profile.name} base_url={profile.base_url}
Intent hints (suggest only if visible on this page and not already used):
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

Name this page. List unmet task constraints that are not on this snapshot.
List at most 2 simple extras that ARE visible. Do not hunt or invent.
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
    unmet = [str(x).strip() for x in (data.get("unmet_constraints") or []) if str(x).strip()][:2]
    extras = [str(x).strip() for x in (data.get("possible_intents") or []) if str(x).strip()][:2]
    result = ObserveResult(
        user_state=str(data.get("user_state") or "").strip(),
        unmet_constraints=unmet,
        possible_intents=extras,
        question=question,
        should_ask=bool(data.get("should_ask")) and bool(question),
        suggested_micro_goal=goal,
        blocker=blocker,
    )
    if pending_question and not replace_question:
        result.should_ask = False
    say("planner", result.user_state or "observing")
    if result.unmet_constraints:
        say("planner", f"Missing: {', '.join(result.unmet_constraints)}")
    if result.should_ask:
        say("planner", f"Ask: {result.question}")
    return result
