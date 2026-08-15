"""Navigator: one Grok call per turn. Proposes a single MCP tool. Does not execute."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from chameleon.llm import complete, parse_json_object
from chameleon.mcp_client import NAVIGATOR_TOOLS
from chameleon.narration import say
from chameleon.profiles import ChecklistItem, SiteProfile
from chameleon.state import GuardianAnswer

NavigatorTool = Literal[
    "browser_navigate",
    "browser_click",
    "browser_type",
    "browser_snapshot",
]

SYSTEM = """You are the Navigator in a three-agent browser automation system.
You propose exactly ONE Playwright MCP tool call per turn. You never execute it.
Allowed tools: browser_navigate, browser_click, browser_type, browser_snapshot.

Playwright MCP click/type use `target` (the snapshot ref such as e12), not CSS selectors.
You may also send `element` as a short human description.

Return ONLY one JSON object. No markdown, no second object, no commentary.
{
  "tool": "browser_click" | "browser_type" | "browser_navigate" | "browser_snapshot" | null,
  "arguments": { ... },
  "reason": "short why",
  "subgoal_complete": false
}

Set subgoal_complete true when the current sub-goal is already done (tool may be null).
Do not invent credentials — use only those provided.
Do not invent personal info (name, email, phone, address). If guardian_answers do not
contain it, return tool=null and subgoal_complete=false — do NOT snapshot-loop. Guardian
will ask the user; on the next turn, fill fields from those answers.
After a successful login, prefer the inventory/app page over logging in again.
The accessibility snapshot is already in the user message — do not call browser_snapshot
unless the previous action changed the page and you have no current snapshot.
For dropdowns/comboboxes: click the control, then browser_type the value from guardian answers.
If a resume/file upload is required and you have no file path in guardian answers, return
tool=null and subgoal_complete=false so Guardian can ask. Mark complete only if optional
or already filled.
"""


class NavigatorProposal(BaseModel):
    tool: NavigatorTool | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    subgoal_complete: bool = False


def navigator_step(
    *,
    subgoal: ChecklistItem,
    snapshot: str,
    history: list[dict[str, Any]],
    guardian_answers: list[GuardianAnswer],
    profile: SiteProfile,
    task: str,
    subgoal_index: int,
    total_subgoals: int,
) -> NavigatorProposal:
    creds = ""
    if profile.requires_login and profile.login:
        creds = (
            f"Login credentials from the site profile (use these, do not ask):\n"
            f"  username: {profile.login.username}\n"
            f"  password: {profile.login.password}\n"
        )
    answers = "\n".join(
        f"- subgoal {a.subgoal_index}: Q: {a.question} A: {a.answer}"
        for a in guardian_answers
    ) or "(none yet)"
    recent = history[-8:]
    prompt = f"""User task: {task}
Site: {profile.name} base_url={profile.base_url}
Sub-goal {subgoal_index + 1}/{total_subgoals}: {subgoal.goal}
Risk tag: {subgoal.risk}

{creds}
Guardian answers so far:
{answers}

Recent executed actions (oldest to newest):
{recent}

Current accessibility snapshot:
{snapshot[:18000]}

Propose the single next tool call.
"""
    raw = complete(prompt, system=SYSTEM)
    data = parse_json_object(raw)
    tool = data.get("tool")
    if tool == "null":
        tool = None
    if tool is not None and tool not in NAVIGATOR_TOOLS:
        raise ValueError(f"Navigator proposed disallowed tool: {tool!r}")
    proposal = NavigatorProposal(
        tool=tool,
        arguments=data.get("arguments") or {},
        reason=str(data.get("reason") or ""),
        subgoal_complete=bool(data.get("subgoal_complete")),
    )
    say(
        "navigator",
        f"{proposal.reason or 'next action'} | tool={proposal.tool} complete={proposal.subgoal_complete}",
    )
    return proposal


COPILOT_SYSTEM = """You are the Navigator in a copilot browser assistant.
Same rules on every copilot site (Maps, OSM, Airbnb, …).
The user already consented to ONE micro-goal. You propose exactly ONE Playwright MCP
tool call per turn. You never execute it.
Allowed tools: browser_navigate, browser_click, browser_type, browser_snapshot.

Playwright MCP click/type use `target` (the snapshot ref such as e12), not CSS selectors.
You may also send `element` as a short human description.

Return ONLY JSON:
{
  "tool": "browser_click" | "browser_type" | "browser_navigate" | "browser_snapshot" | null,
  "arguments": { ... },
  "reason": "short why",
  "subgoal_complete": false
}

Core vs optional:
- Core: search box / query, dates, guests, then submit Search. Do these.
- Optional (stars, Guest favorite, chips, price, Open now, layers, sort): apply
  ONLY if that control is already visible in THIS snapshot. One try. If it is
  not in the snapshot, skip it — set subgoal_complete true after core search.
  Do NOT open Filters / More / nested menus hunting for a missing control.

Rules:
- Complete the consented micro-goal, then set subgoal_complete true (tool may be null).
- A filled search form is NOT complete until Search/Apply is clicked.
- Mark complete when results/place/listing is visible, even if optional filters were skipped.
- Guest steppers and date cells are core steps — keep going until the goal page is showing.
- If you cannot find the next CORE control, set tool null and subgoal_complete true.
- Do NOT click the map canvas. Search box, sidebar, cards, visible filters, directions only.
- Do not invent extra goals. No booking, paying, sharing, or leaving the site.
- Prefer typing in the visible search box over a crafted URL.
- Do not call browser_snapshot if a current snapshot is already in the prompt.
- Keep reason under 12 words.
"""


def navigator_copilot_step(
    *,
    micro_goal: str,
    snapshot: str,
    history: list[dict[str, Any]],
    profile: SiteProfile,
    task: str,
) -> NavigatorProposal:
    hints = "\n".join(f"- {h}" for h in profile.intent_hints) or "(none)"
    recent = history[-20:]
    prompt = f"""User task: {task}
Site: {profile.name} base_url={profile.base_url}
Consented micro-goal: {micro_goal}
Optional extras (apply only if already visible; never hunt):
{hints}

Recent executed actions (oldest to newest):
{recent}

Current accessibility snapshot:
{snapshot[:18000]}

Propose the single next tool call, or mark the micro-goal complete.
"""
    raw = complete(prompt, system=COPILOT_SYSTEM)
    data = parse_json_object(raw)
    tool = data.get("tool")
    if tool == "null":
        tool = None
    if tool is not None and tool not in NAVIGATOR_TOOLS:
        raise ValueError(f"Navigator proposed disallowed tool: {tool!r}")
    proposal = NavigatorProposal(
        tool=tool,
        arguments=data.get("arguments") or {},
        reason=str(data.get("reason") or ""),
        subgoal_complete=bool(data.get("subgoal_complete")),
    )
    say(
        "navigator",
        f"{proposal.reason or 'micro-goal'} | tool={proposal.tool} complete={proposal.subgoal_complete}",
    )
    return proposal
