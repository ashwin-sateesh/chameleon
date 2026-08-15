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

Return ONLY JSON:
{
  "tool": "browser_click" | "browser_type" | "browser_navigate" | "browser_snapshot" | null,
  "arguments": { ... },
  "reason": "short why",
  "subgoal_complete": false
}

Set subgoal_complete true when the current sub-goal is already done (tool may be null).
Do not invent credentials — use only those provided.
Do not skip Guardian-sensitive work by guessing when answers are missing.
After a successful login, prefer the inventory/app page over logging in again.
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
