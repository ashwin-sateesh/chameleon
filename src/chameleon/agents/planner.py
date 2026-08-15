"""Planner: checklist from the site profile template. No MCP. No Grok in this build."""

from __future__ import annotations

from chameleon.narration import say
from chameleon.profiles import ChecklistItem, SiteProfile


def planner_plan(task: str, site_profile: SiteProfile) -> list[ChecklistItem]:
    checklist = [item.model_copy() for item in site_profile.checklist_template]
    say("planner", f"Task: {task}")
    say("planner", f"Site: {site_profile.name} ({site_profile.id})")
    for index, item in enumerate(checklist, start=1):
        say("planner", f"{index}. [{item.risk}] {item.goal}")
    return checklist
