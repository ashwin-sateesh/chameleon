"""In-process UI bridge: logs, frames, Guardian ASK, hold. CLI leaves this unset."""

from __future__ import annotations

import asyncio
import json
from contextvars import ContextVar
from typing import Any


def _json_safe(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return str(value)


class UiBridge:
    def __init__(self) -> None:
        self.subscribers: list[asyncio.Queue[dict[str, Any]]] = []
        self.stop = False
        self._answer: asyncio.Future[str] | None = None
        self._dismiss: asyncio.Future[None] | None = None
        self.mcp: Any = None
        self.live: Any = None
        self._input_lock = asyncio.Lock()

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.subscribers.append(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        if queue in self.subscribers:
            self.subscribers.remove(queue)

    def emit(self, event_type: str, **payload: Any) -> None:
        message = {"type": event_type, **{k: _json_safe(v) for k, v in payload.items()}}
        for queue in list(self.subscribers):
            try:
                queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def request_stop(self) -> None:
        self.stop = True
        self.submit_answer("")
        self.dismiss()

    async def wait_answer(self, question: str, options: list[str] | None = None) -> str:
        loop = asyncio.get_running_loop()
        self._answer = loop.create_future()
        self.emit("ask", question=question, options=list(options or []))
        try:
            return await self._answer
        finally:
            self._answer = None

    def submit_answer(self, text: str) -> None:
        if self._answer is not None and not self._answer.done():
            self._answer.set_result(text)

    async def wait_dismiss(self) -> None:
        loop = asyncio.get_running_loop()
        self._dismiss = loop.create_future()
        self.emit("hold")
        try:
            await self._dismiss
        finally:
            self._dismiss = None

    def dismiss(self) -> None:
        if self._dismiss is not None and not self._dismiss.done():
            self._dismiss.set_result(None)

    def waiting_ask(self) -> bool:
        return self._answer is not None and not self._answer.done()

    def waiting_hold(self) -> bool:
        return self._dismiss is not None and not self._dismiss.done()

    async def user_click(self, x: int, y: int) -> None:
        live = self.live
        if live is not None:
            try:
                async with self._input_lock:
                    await live.click(x, y)
            except Exception as exc:  # noqa: BLE001
                self.emit("error", message=f"Click failed: {exc}")
            return
        mcp = self.mcp
        if mcp is None:
            self.emit("error", message="No live page to click yet.")
            return
        try:
            async with self._input_lock:
                await mcp.click_xy(x, y)
                src = await mcp.screenshot_data_url()
                if src:
                    self.emit("frame", src=src)
        except Exception as exc:  # noqa: BLE001
            self.emit("error", message=f"Click failed: {exc}")

    async def user_key(self, key: str) -> None:
        live = self.live
        if live is not None:
            try:
                async with self._input_lock:
                    await live.key(key)
            except Exception as exc:  # noqa: BLE001
                self.emit("error", message=f"Key failed: {exc}")
            return
        mcp = self.mcp
        if mcp is None or not key:
            return
        try:
            async with self._input_lock:
                await mcp.press_key(key)
                src = await mcp.screenshot_data_url()
                if src:
                    self.emit("frame", src=src)
        except Exception as exc:  # noqa: BLE001
            self.emit("error", message=f"Key failed: {exc}")

    async def user_scroll(self, x: int, y: int, delta_x: float, delta_y: float) -> None:
        live = self.live
        if live is None:
            return
        try:
            async with self._input_lock:
                await live.scroll(x, y, delta_x, delta_y)
        except Exception as exc:  # noqa: BLE001
            self.emit("error", message=f"Scroll failed: {exc}")

    async def user_goto(self, url: str) -> None:
        live = self.live
        if live is None:
            self.emit("error", message="No live page to navigate yet.")
            return
        try:
            async with self._input_lock:
                await live.goto(url)
        except Exception as exc:  # noqa: BLE001
            self.emit("error", message=f"Navigate failed: {exc}")

    async def user_nav(self, action: str) -> None:
        live = self.live
        if live is None:
            return
        if action not in {"back", "forward", "reload"}:
            return
        try:
            async with self._input_lock:
                await live.nav(action)
        except Exception as exc:  # noqa: BLE001
            self.emit("error", message=f"Navigation failed: {exc}")


current_bridge: ContextVar[UiBridge | None] = ContextVar("chameleon_bridge", default=None)


def emit(event_type: str, **payload: Any) -> None:
    bridge = current_bridge.get()
    if bridge is not None:
        bridge.emit(event_type, **payload)


def dumps_event(event: dict[str, Any]) -> str:
    return json.dumps(event)
