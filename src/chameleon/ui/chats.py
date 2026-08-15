"""One chat transcript per task_id, stored under data/chats/."""

from __future__ import annotations

import re
import uuid
from datetime import datetime, timezone

from pydantic import BaseModel, Field

from chameleon.paths import chats_dir
from chameleon.state import load_state

_SAFE_ID = re.compile(r"^[A-Za-z0-9._-]+$")


class ChatMessage(BaseModel):
    agent: str
    message: str
    at: str = ""


class ChatRecord(BaseModel):
    task_id: str
    title: str = "New chat"
    created_at: str = ""
    updated_at: str = ""
    messages: list[ChatMessage] = Field(default_factory=list)


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_task_id() -> str:
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"chat-{stamp}-{uuid.uuid4().hex[:4]}"


def valid_task_id(task_id: str) -> bool:
    return bool(task_id) and bool(_SAFE_ID.match(task_id))


def chat_path(task_id: str):
    return chats_dir() / f"{task_id}.json"


def load_chat(task_id: str) -> ChatRecord | None:
    if not valid_task_id(task_id):
        return None
    path = chat_path(task_id)
    if not path.is_file():
        return None
    return ChatRecord.model_validate_json(path.read_text())


def save_chat(record: ChatRecord) -> None:
    path = chat_path(record.task_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record.updated_at = now_iso()
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(record.model_dump_json(indent=2) + "\n")
    tmp.replace(path)


def _placeholder_title(title: str | None) -> bool:
    return not (title or "").strip() or title.strip() == "New chat"


def chat_title(record, state=None) -> str:
    if record is not None and not _placeholder_title(record.title):
        return record.title.strip()[:80]
    if record is not None:
        for item in record.messages:
            if item.agent in {"user", "answer"} and item.message.strip():
                return item.message.strip()[:80]
    task = (getattr(state, "task", None) or "").strip()
    if task:
        return task[:80]
    return "New chat"


def ensure_chat(task_id: str, title: str | None = None) -> ChatRecord:
    if not valid_task_id(task_id):
        raise ValueError(f"Invalid task_id {task_id!r}")
    record = load_chat(task_id)
    state = load_state(task_id)
    resolved = (title or "").strip()[:80] or chat_title(record, state)
    if record is None:
        stamp = now_iso()
        record = ChatRecord(
            task_id=task_id,
            title=resolved,
            created_at=stamp,
            updated_at=stamp,
        )
        save_chat(record)
        return record
    if _placeholder_title(record.title) and resolved != record.title:
        record.title = resolved
        save_chat(record)
    return record


def create_chat() -> ChatRecord:
    return ensure_chat(new_task_id())


def append_message(task_id: str, agent: str, message: str) -> ChatRecord:
    record = ensure_chat(task_id)
    text = message.strip()
    if not text:
        return record
    if record.messages and record.messages[-1].agent == agent and record.messages[-1].message == text:
        return record
    record.messages.append(ChatMessage(agent=agent, message=text, at=now_iso()))
    if agent in {"user", "answer"} and _placeholder_title(record.title):
        record.title = text[:80]
    save_chat(record)
    return record


def _task_ids() -> set[str]:
    chats = chats_dir()
    if not chats.is_dir():
        return set()
    return {path.stem for path in chats.glob("*.json") if valid_task_id(path.stem)}


def chat_summaries() -> list[dict]:
    items: list[dict] = []
    for task_id in _task_ids():
        record = load_chat(task_id)
        state = load_state(task_id)
        title = chat_title(record, state)
        updated = (record.updated_at if record else "") or ""
        items.append(
            {
                "task_id": task_id,
                "title": title,
                "status": state.status.value if state else "new",
                "site": state.site if state else None,
                "updated_at": updated,
            }
        )
    items.sort(key=lambda row: row["updated_at"] or row["task_id"], reverse=True)
    return items


def empty_draft() -> ChatRecord | None:
    for item in chat_summaries():
        if item["status"] != "new":
            continue
        record = load_chat(item["task_id"])
        if record is not None and not record.messages:
            return record
    return None
