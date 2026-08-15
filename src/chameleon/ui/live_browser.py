"""Live Chromium session streamed into the UI via CDP screencast."""

from __future__ import annotations

import asyncio
import json
import os
import socket
from pathlib import Path
from typing import Any, Callable
from urllib.error import URLError
from urllib.request import urlopen

EmitFn = Callable[..., None]

_CHROME_GLOBS = (
    "chromium-*/chrome-mac-arm64/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "chromium-*/chrome-mac/Google Chrome for Testing.app/Contents/MacOS/Google Chrome for Testing",
    "chromium-*/chrome-linux/chrome",
    "chromium-*/chrome-win64/chrome.exe",
    "chromium-*/chrome-win/chrome.exe",
)


def _free_port() -> int:
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def _json_get(url: str) -> Any:
    with urlopen(url, timeout=1.5) as response:
        return json.loads(response.read().decode())


def _browser_cache_roots() -> list[Path]:
    roots: list[Path] = []
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if env:
        roots.append(Path(env))
    roots.append(Path.home() / "Library" / "Caches" / "ms-playwright")
    roots.append(Path.home() / ".cache" / "ms-playwright")
    seen: set[Path] = set()
    unique: list[Path] = []
    for root in roots:
        if root in seen:
            continue
        seen.add(root)
        unique.append(root)
    return unique


def _find_chromium(root: Path) -> Path | None:
    if not root.is_dir():
        return None
    matches: list[Path] = []
    for pattern in _CHROME_GLOBS:
        matches.extend(path for path in root.glob(pattern) if path.is_file())
    return matches[-1] if matches else None


def _drop_stale_browsers_path() -> None:
    """Cursor sandboxes set PLAYWRIGHT_BROWSERS_PATH to a cache that often has no Chromium."""
    env = os.environ.get("PLAYWRIGHT_BROWSERS_PATH")
    if not env:
        return
    if _find_chromium(Path(env)):
        return
    os.environ.pop("PLAYWRIGHT_BROWSERS_PATH", None)


async def _chromium_executable() -> str:
    from playwright.async_api import async_playwright

    _drop_stale_browsers_path()
    playwright = await async_playwright().start()
    try:
        exe = Path(playwright.chromium.executable_path)
    finally:
        await playwright.stop()
    if exe.is_file():
        return str(exe)
    for root in _browser_cache_roots():
        found = _find_chromium(root)
        if found:
            return str(found)
    raise RuntimeError(
        f"Chromium not found at {exe}. From the project venv run: playwright install chromium"
    )


class LiveBrowser:
    def __init__(self, *, user_data_dir: Path, emit: EmitFn) -> None:
        self.user_data_dir = user_data_dir
        self.emit = emit
        self.port = _free_port()
        self.endpoint = f"http://127.0.0.1:{self.port}"
        self._proc: asyncio.subprocess.Process | None = None
        self._task: asyncio.Task[None] | None = None
        self._ws: Any = None
        self._cdp_id = 0
        self._stop = asyncio.Event()
        self._page_url = ""
        self._pending: dict[int, asyncio.Future] = {}

    async def start(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        exe = await _chromium_executable()
        self.emit("browser", state="connecting")
        self._proc = await asyncio.create_subprocess_exec(
            exe,
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.user_data_dir}",
            "--headless=new",
            "--no-first-run",
            "--no-default-browser-check",
            "--window-size=1280,720",
            "--force-device-scale-factor=1",
            "--hide-scrollbars",
            "about:blank",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        await self._wait_cdp()
        self._task = asyncio.create_task(self._session_loop(), name="cdp-live")

    async def close(self) -> None:
        self._stop.set()
        self._fail_pending(RuntimeError("Live browser closed"))
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        if self._ws is not None:
            try:
                await self._ws.close()
            except Exception:
                pass
        if self._proc and self._proc.returncode is None:
            self._proc.terminate()
            try:
                await asyncio.wait_for(self._proc.wait(), timeout=3)
            except (TimeoutError, asyncio.TimeoutError):
                self._proc.kill()

    async def click(self, x: int, y: int) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Live browser is not ready")
        for event_type in ("mousePressed", "mouseReleased"):
            await self._send(
                ws,
                "Input.dispatchMouseEvent",
                {"type": event_type, "x": x, "y": y, "button": "left", "clickCount": 1},
            )

    async def scroll(self, x: int, y: int, delta_x: float, delta_y: float) -> None:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Live browser is not ready")
        await self._send(
            ws,
            "Input.dispatchMouseEvent",
            {"type": "mouseWheel", "x": x, "y": y, "deltaX": delta_x, "deltaY": delta_y},
        )

    async def goto(self, url: str) -> None:
        ws = self._ws
        if ws is None or not url.strip():
            return
        target = url.strip()
        if "://" not in target:
            target = "https://" + target
        await self._send(ws, "Page.navigate", {"url": target})
        self._emit_url(target)

    async def nav(self, action: str) -> None:
        ws = self._ws
        if ws is None:
            return
        if action == "reload":
            await self._send(ws, "Page.reload")
            return
        script = "history.back()" if action == "back" else "history.forward()"
        await self._send(ws, "Runtime.evaluate", {"expression": script})

    async def key(self, key: str) -> None:
        ws = self._ws
        if ws is None or not key:
            return
        params: dict[str, Any] = {"type": "keyDown", "key": key}
        if len(key) == 1:
            params["text"] = key
            params["type"] = "char"
            await self._send(ws, "Input.dispatchKeyEvent", params)
            return
        await self._send(ws, "Input.dispatchKeyEvent", {"type": "keyDown", "key": key})
        await self._send(ws, "Input.dispatchKeyEvent", {"type": "keyUp", "key": key})

    async def evaluate(self, expression: str) -> Any:
        ws = self._ws
        if ws is None:
            raise RuntimeError("Live browser is not ready")
        result = await self._call(
            ws,
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
            timeout=8,
        )
        if result.get("exceptionDetails"):
            raise RuntimeError(str(result["exceptionDetails"]))
        inner = result.get("result") or {}
        return inner.get("value")

    async def dump_page_state(self) -> dict[str, Any]:
        from chameleon.page_state import DUMP_JS

        data = await self.evaluate(DUMP_JS)
        return data if isinstance(data, dict) else {}

    async def restore_page_state(self, page_state: dict[str, Any]) -> None:
        from chameleon.page_state import restore_js

        if not page_state:
            return
        await self.evaluate(restore_js(page_state))

    async def _wait_cdp(self) -> None:
        url = f"{self.endpoint}/json/version"
        for _ in range(80):
            try:
                _json_get(url)
                return
            except (URLError, TimeoutError, OSError, json.JSONDecodeError):
                await asyncio.sleep(0.1)
        raise RuntimeError("Chromium CDP did not start")

    def _page_ws(self) -> str | None:
        try:
            targets = _json_get(f"{self.endpoint}/json/list")
        except (URLError, TimeoutError, OSError, json.JSONDecodeError):
            return None
        pages = [t for t in targets if t.get("type") == "page" and t.get("webSocketDebuggerUrl")]
        if not pages:
            return None
        pages.sort(key=lambda t: 0 if str(t.get("url", "")).startswith("http") else 1)
        url = str(pages[0].get("url") or "")
        if url:
            self._emit_url(url)
        return pages[0]["webSocketDebuggerUrl"]

    def _emit_url(self, url: str) -> None:
        if url and url != self._page_url:
            self._page_url = url
            self.emit("browser", url=url, state="live")

    def _fail_pending(self, exc: BaseException) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    async def _send(self, ws: Any, method: str, params: dict[str, Any] | None = None) -> int:
        self._cdp_id += 1
        message_id = self._cdp_id
        payload: dict[str, Any] = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        await ws.send(json.dumps(payload))
        return message_id

    async def _call(
        self, ws: Any, method: str, params: dict[str, Any] | None = None, timeout: float = 4
    ) -> dict[str, Any]:
        loop = asyncio.get_running_loop()
        message_id = await self._send(ws, method, params)
        fut: asyncio.Future = loop.create_future()
        self._pending[message_id] = fut
        try:
            message = await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(message_id, None)
        if message.get("error"):
            raise RuntimeError(str(message["error"]))
        return message.get("result") or {}

    async def _session_loop(self) -> None:
        import websockets

        attached: str | None = None
        while not self._stop.is_set():
            page_ws = self._page_ws()
            if not page_ws:
                await asyncio.sleep(0.2)
                continue
            if page_ws == attached and self._ws is not None:
                await asyncio.sleep(0.25)
                continue
            attached = page_ws
            try:
                async with websockets.connect(page_ws, max_size=8_000_000, open_timeout=5) as ws:
                    self._ws = ws
                    self._cdp_id = 0
                    self._pending.clear()
                    pump = asyncio.create_task(self._pump(ws), name="cdp-pump")
                    try:
                        await self._call(ws, "Page.enable")
                        await self._call(ws, "Runtime.enable")
                        await self._call(
                            ws,
                            "Emulation.setDeviceMetricsOverride",
                            {
                                "width": 1280,
                                "height": 720,
                                "deviceScaleFactor": 1,
                                "mobile": False,
                            },
                        )
                        self.emit("browser", state="live", url=self._page_url)
                        ticks = 0
                        while not self._stop.is_set() and not pump.done():
                            ticks += 1
                            if ticks % 6 == 0:
                                nxt = self._page_ws()
                                if nxt and nxt != attached:
                                    break
                            try:
                                result = await self._call(
                                    ws,
                                    "Page.captureScreenshot",
                                    {"format": "jpeg", "quality": 52},
                                    timeout=3,
                                )
                            except Exception:
                                break
                            data = result.get("data")
                            if data:
                                payload: dict[str, Any] = {
                                    "src": f"data:image/jpeg;base64,{data}"
                                }
                                if self._page_url:
                                    payload["url"] = self._page_url
                                self.emit("frame", **payload)
                            await asyncio.sleep(0.16)
                    finally:
                        pump.cancel()
                        try:
                            await pump
                        except (asyncio.CancelledError, Exception):
                            pass
                        self._fail_pending(RuntimeError("CDP target changed"))
                        if self._ws is ws:
                            self._ws = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self._ws = None
                attached = None
                self.emit("browser", state="reconnecting")
                await asyncio.sleep(0.4)

    async def _pump(self, ws: Any) -> None:
        async for raw in ws:
            if self._stop.is_set():
                return
            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                continue
            message_id = message.get("id")
            if message_id in self._pending:
                fut = self._pending[message_id]
                if not fut.done():
                    fut.set_result(message)
                continue
            method = message.get("method")
            params = message.get("params") or {}
            if method == "Page.frameNavigated":
                frame = params.get("frame") or {}
                if not frame.get("parentId") and frame.get("url"):
                    self._emit_url(str(frame["url"]))
            elif method == "Page.navigatedWithinDocument":
                url = params.get("url")
                if url:
                    self._emit_url(str(url))

