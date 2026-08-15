"""Local two-pane web console: live Playwright frames + agent chat."""

from __future__ import annotations

import asyncio
import json
import webbrowser
from pathlib import Path
from typing import Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from chameleon.ask_options import extract_ask_options
from chameleon.loop import run_task
from chameleon.profiles import UnknownSiteError, load_profile
from chameleon.state import TaskStatus, load_state
from chameleon.ui.chats import (
    append_message,
    chat_summaries,
    create_chat,
    empty_draft,
    ensure_chat,
    load_chat,
    valid_task_id,
)
from chameleon.ui.events import UiBridge, dumps_event
from chameleon.ui.intent import infer_site, is_resume, is_stop, load_profiles

STATIC_DIR = Path(__file__).resolve().parent / "static"


class Console:
    def __init__(self) -> None:
        self.bridge = UiBridge()
        self.task: asyncio.Task[Any] | None = None
        self.pending_task: str | None = None
        self.active_chat_id: str | None = None
        self.queued: tuple[str, str, str | None] | None = None
        original = self.bridge.emit

        def _emit(event_type: str, **payload: Any) -> None:
            original(event_type, **payload)
            self._persist_event(event_type, payload)

        self.bridge.emit = _emit  # type: ignore[method-assign]

    def busy(self) -> bool:
        return self.task is not None and not self.task.done()

    def _persist_event(self, event_type: str, payload: dict[str, Any]) -> None:
        if not self.active_chat_id:
            return
        if event_type == "log":
            append_message(
                self.active_chat_id,
                str(payload.get("agent") or "system"),
                str(payload.get("message") or ""),
            )
        elif event_type == "ask":
            append_message(self.active_chat_id, "ask", str(payload.get("question") or ""))
        elif event_type == "error":
            append_message(self.active_chat_id, "system", str(payload.get("message") or ""))


console = Console()


async def index(_: Request) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


async def profiles(_: Request) -> JSONResponse:
    items = []
    for profile in load_profiles():
        items.append({"id": profile.id, "name": profile.name, "task_type": profile.task_type})
    return JSONResponse({"profiles": items})


async def chats(_: Request) -> JSONResponse:
    return JSONResponse({"chats": chat_summaries(), "active_id": console.active_chat_id})


async def websocket_endpoint(ws: WebSocket) -> None:
    await ws.accept()
    queue = console.bridge.subscribe()
    if console.active_chat_id is None:
        draft = empty_draft() or create_chat()
        console.active_chat_id = draft.task_id
    _emit_session()
    sender = asyncio.create_task(_pump(ws, queue))
    try:
        while True:
            raw = await ws.receive_text()
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            await _handle_client(message)
    except WebSocketDisconnect:
        pass
    finally:
        sender.cancel()
        console.bridge.unsubscribe(queue)
        if not console.busy():
            console.bridge.dismiss()


async def _pump(ws: WebSocket, queue: asyncio.Queue) -> None:
    try:
        while True:
            event = await queue.get()
            await ws.send_text(dumps_event(event))
    except Exception:  # noqa: BLE001
        return


def _emit_chats() -> None:
    console.bridge.emit(
        "chats",
        active_id=console.active_chat_id,
        chats=chat_summaries(),
    )


def _emit_session() -> None:
    task_id = console.active_chat_id
    record = load_chat(task_id) if task_id else None
    state = load_state(task_id) if task_id else None
    pending = state.pending_question if state else None
    pending_options = list(state.pending_options) if state and pending else []
    if pending and not pending_options:
        pending_options = extract_ask_options(pending)
    console.bridge.emit(
        "session",
        active_id=task_id,
        chats=chat_summaries(),
        messages=[m.model_dump() for m in (record.messages if record else [])],
        pending_question=pending,
        pending_options=pending_options,
        status=state.status.value if state else "new",
        site=state.site if state else None,
        current_url=state.current_url if state else None,
    )


async def _handle_client(message: dict[str, Any]) -> None:
    kind = message.get("type")
    if kind == "chat":
        await _handle_chat(str(message.get("text") or "").strip())
    elif kind == "new_chat":
        await _new_chat()
    elif kind == "open_chat":
        await _open_chat(str(message.get("task_id") or "").strip())
    elif kind == "pick_site":
        site = str(message.get("site") or "").strip()
        task = console.pending_task or str(message.get("task") or "").strip()
        if site and task:
            console.pending_task = None
            try:
                label = load_profile(site).name
            except UnknownSiteError:
                label = site
            console.bridge.emit("log", agent="user", message=label)
            await _start_or_queue(site, task)
    elif kind == "answer":
        console.bridge.submit_answer(str(message.get("text") or ""))
    elif kind == "dismiss":
        console.bridge.dismiss()
    elif kind == "stop":
        console.bridge.request_stop()
    elif kind == "pointer":
        asyncio.create_task(
            console.bridge.user_click(int(message.get("x") or 0), int(message.get("y") or 0))
        )
    elif kind == "key":
        asyncio.create_task(console.bridge.user_key(str(message.get("key") or "")))
    elif kind == "scroll":
        asyncio.create_task(
            console.bridge.user_scroll(
                int(message.get("x") or 0),
                int(message.get("y") or 0),
                float(message.get("deltaX") or 0),
                float(message.get("deltaY") or 0),
            )
        )
    elif kind == "goto":
        asyncio.create_task(console.bridge.user_goto(str(message.get("url") or "")))
    elif kind == "nav":
        asyncio.create_task(console.bridge.user_nav(str(message.get("action") or "")))


async def _new_chat() -> None:
    await _stop_current()
    console.pending_task = None
    draft = empty_draft() or create_chat()
    console.active_chat_id = draft.task_id
    _emit_session()


async def _open_chat(task_id: str) -> None:
    if not valid_task_id(task_id):
        console.bridge.emit("error", message="Invalid chat id.")
        return
    if task_id == console.active_chat_id and not console.busy():
        _emit_session()
        return
    await _stop_current()
    console.pending_task = None
    state = load_state(task_id)
    ensure_chat(task_id, title=state.task if state else None)
    console.active_chat_id = task_id
    _emit_session()
    if state is not None and state.status in {TaskStatus.running, TaskStatus.paused_ask}:
        _launch(state.site, state.task, task_id)


async def _stop_current() -> None:
    console.queued = None
    task = console.task
    if task is not None and not task.done():
        console.bridge.request_stop()
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=12)
        except (TimeoutError, asyncio.CancelledError, Exception):  # noqa: BLE001
            task.cancel()
            try:
                await task
            except Exception:  # noqa: BLE001
                pass
    else:
        console.bridge.dismiss()
    console.bridge.stop = False
    console.task = None


async def _handle_chat(text: str) -> None:
    if not text:
        return
    if console.bridge.waiting_ask():
        console.bridge.submit_answer(text)
        return

    if is_stop(text):
        console.bridge.emit("log", agent="user", message=text)
        if console.busy():
            console.bridge.request_stop()
        return

    if console.active_chat_id is None:
        console.active_chat_id = (empty_draft() or create_chat()).task_id

    existing = load_state(console.active_chat_id)
    if console.bridge.waiting_hold() or (
        existing is not None and existing.status == TaskStatus.completed
    ):
        await _new_chat()

    if console.busy():
        console.bridge.emit("log", agent="user", message=text)
        console.bridge.emit("log", agent="system", message="Still working — I'll ask if I need you.")
        return

    console.bridge.emit("log", agent="user", message=text)

    site, task, task_id = _resolve_start(text)
    if not site or not task:
        _emit_session()
        return
    _launch(site, task, task_id)


def _resolve_start(text: str) -> tuple[str | None, str | None, str | None]:
    if is_resume(text):
        existing = load_state(console.active_chat_id) if console.active_chat_id else None
        if existing is None:
            console.bridge.emit("log", agent="system", message="Nothing to resume yet.")
            return None, None, None
        return existing.site, existing.task, existing.task_id

    profiles = load_profiles()
    site = infer_site(text, profiles)
    task = text
    if console.pending_task and site:
        task = console.pending_task
        console.pending_task = None
    if site:
        return site, task, console.active_chat_id

    console.pending_task = console.pending_task or text
    items = [{"id": p.id, "name": p.name} for p in profiles]
    names = " or ".join(p.name for p in profiles) or "a configured site"
    console.bridge.emit(
        "need_site",
        profiles=items,
        message=f"Which site should I open — {names}?",
    )
    console.bridge.emit("log", agent="system", message=f"Which site should I open — {names}?")
    return None, None, None


async def _start_or_queue(site: str, task: str, task_id: str | None = None) -> None:
    if console.bridge.waiting_ask():
        return
    if console.busy():
        if console.bridge.waiting_hold():
            console.queued = (site, task, task_id)
            console.bridge.dismiss()
            return
        console.bridge.emit("log", agent="system", message="Still working — I'll ask if I need you.")
        return
    _launch(site, task, task_id)


def _launch(site: str, task: str, task_id: str | None = None) -> None:
    if console.busy():
        console.bridge.emit("error", message="A task is already running.")
        return
    task_id = task_id or console.active_chat_id or create_chat().task_id
    existing = load_state(task_id)
    if existing is not None and existing.status == TaskStatus.completed:
        task_id = create_chat().task_id
    console.active_chat_id = task_id
    ensure_chat(task_id, title=task)
    console.bridge.stop = False
    try:
        name = load_profile(site).name
    except UnknownSiteError as exc:
        console.bridge.emit("error", message=str(exc))
        return
    console.bridge.emit("log", agent="system", message=f"On it — {name}.")
    _emit_chats()
    console.task = asyncio.create_task(
        _run(site, task, task_id),
        name=f"chameleon-{task_id}",
    )


async def _run(site: str, task: str, task_id: str) -> None:
    try:
        await run_task(site, task, task_id, headless=True, bridge=console.bridge)
    except UnknownSiteError as exc:
        console.bridge.emit("error", message=str(exc))
    except Exception as exc:  # noqa: BLE001
        console.bridge.emit("error", message=str(exc))
    finally:
        console.bridge.emit("idle")
        _emit_session()
        queued = console.queued
        console.queued = None
        if queued:
            next_site, next_task, next_id = queued
            _launch(next_site, next_task, next_id)


def create_app() -> Starlette:
    return Starlette(
        routes=[
            Route("/", index),
            Route("/api/profiles", profiles),
            Route("/api/chats", chats),
            WebSocketRoute("/ws", websocket_endpoint),
        ]
    )


def serve(host: str = "127.0.0.1", port: int = 8765, open_browser: bool = True) -> None:
    import uvicorn

    url = f"http://{host}:{port}"
    print(f"Chameleon console → {url}", flush=True)
    if open_browser:
        webbrowser.open(url)
    uvicorn.run(create_app(), host=host, port=port, log_level="info")
