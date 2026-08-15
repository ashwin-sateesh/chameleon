from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field

from chameleon.paths import storage_state_path, task_state_path
from chameleon.profiles import ChecklistItem


class TaskStatus(str, Enum):
    running = "running"
    paused_ask = "paused_ask"
    completed = "completed"
    failed = "failed"


class ActionRecord(BaseModel):
    tool: str
    arguments: dict[str, Any] = Field(default_factory=dict)
    reason: str = ""
    result_excerpt: str = ""


class GuardianAnswer(BaseModel):
    subgoal_index: int
    question: str
    answer: str


class TaskState(BaseModel):
    site: str
    task: str
    task_id: str
    planner_checklist: list[ChecklistItem] = Field(default_factory=list)
    current_subgoal_index: int = 0
    navigator_action_history: list[ActionRecord] = Field(default_factory=list)
    guardian_answers: list[GuardianAnswer] = Field(default_factory=list)
    current_url: str | None = None
    storage_state_path: str | None = None
    status: TaskStatus = TaskStatus.running
    pending_question: str | None = None
    pending_options: list[str] = Field(default_factory=list)
    error: str | None = None
    page_state: dict[str, Any] | None = None


def save_state(state: TaskState) -> None:
    path = task_state_path(state.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(state.model_dump_json(indent=2) + "\n")
    tmp.replace(path)


def load_state(task_id: str) -> TaskState | None:
    path = task_state_path(task_id)
    if not path.is_file():
        return None
    return TaskState.model_validate_json(path.read_text())


def new_state(
    *,
    site: str,
    task: str,
    task_id: str,
    checklist: list[ChecklistItem],
    current_url: str | None = None,
) -> TaskState:
    dump = storage_state_path(task_id)
    dump.parent.mkdir(parents=True, exist_ok=True)
    return TaskState(
        site=site,
        task=task,
        task_id=task_id,
        planner_checklist=checklist,
        current_url=current_url,
        storage_state_path=str(dump),
        status=TaskStatus.running,
    )
