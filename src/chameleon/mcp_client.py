"""Playwright MCP stdio client. Orchestrator-owned; agents do not import this except Navigator proposals."""

from __future__ import annotations

import base64
import json
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from chameleon.fingerprint import extract_url

__all__ = [
    "NAVIGATOR_TOOLS",
    "PlaywrightMCP",
    "extract_url",
    "find_ref",
    "normalize_tool_arguments",
    "tool_result_text",
]

NAVIGATOR_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_snapshot",
    }
)


def find_ref(snapshot: str, label: str) -> str | None:
    """Return the snapshot target/ref on the line that mentions label."""
    quoted = re.compile(
        rf'["\']{re.escape(label)}["\'][^\n]*\[(?:ref=)?([a-zA-Z0-9_-]+)\]',
        re.IGNORECASE,
    )
    match = quoted.search(snapshot)
    if match:
        return match.group(1)
    for line in snapshot.splitlines():
        if label.lower() in line.lower():
            ref = re.search(r"\[(?:ref=)?([a-zA-Z0-9_-]+)\]", line)
            if ref:
                return ref.group(1)
    return None


def normalize_tool_arguments(tool: str, arguments: dict[str, Any]) -> dict[str, Any]:
    args = dict(arguments)
    if "ref" in args and "target" not in args:
        args["target"] = args.pop("ref")
    if "selector" in args and "target" not in args:
        args["target"] = args.pop("selector")
    if tool in {"browser_click", "browser_type"} and "target" not in args:
        raise ValueError(f"{tool} requires target (or ref) from the latest snapshot")
    return args


def tool_result_text(result: Any) -> str:
    parts: list[str] = []
    content = getattr(result, "content", None) or []
    for item in content:
        if getattr(item, "type", None) == "text":
            parts.append(getattr(item, "text", "") or "")
        elif isinstance(item, dict) and item.get("type") == "text":
            parts.append(str(item.get("text") or ""))
    if not parts and result is not None:
        structured = getattr(result, "structuredContent", None)
        if structured:
            parts.append(json.dumps(structured))
    text = "\n".join(p for p in parts if p)
    if getattr(result, "isError", False):
        raise RuntimeError(text or "MCP tool error")
    return text


class PlaywrightMCP:
    def __init__(
        self,
        *,
        user_data_dir: Path,
        output_dir: Path,
        headless: bool = False,
        cdp_endpoint: str | None = None,
    ) -> None:
        self.user_data_dir = user_data_dir
        self.output_dir = output_dir
        self.headless = headless
        self.cdp_endpoint = cdp_endpoint
        self._stack = AsyncExitStack()
        self.client: Any = None

    async def __aenter__(self) -> PlaywrightMCP:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        from mcp import Client, StdioServerParameters, stdio_client

        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        args = [
            "-y",
            "@playwright/mcp@latest",
            "--output-dir",
            str(self.output_dir),
            "--caps",
            "storage,vision",
            "--viewport-size",
            "1280x720",
        ]
        if self.cdp_endpoint:
            args.extend(["--cdp-endpoint", self.cdp_endpoint])
        else:
            args.extend(["--user-data-dir", str(self.user_data_dir)])
            if self.headless:
                args.append("--headless")
        params = StdioServerParameters(
            command="npx",
            args=args,
        )
        self.client = await self._stack.enter_async_context(Client(stdio_client(params)))

    async def close(self) -> None:
        self.client = None
        await self._stack.aclose()
        self._stack = AsyncExitStack()

    async def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> str:
        if self.client is None:
            raise RuntimeError("Playwright MCP is not started")
        result = await self.client.call_tool(name, arguments or {})
        return tool_result_text(result)

    async def snapshot(self) -> str:
        return await self.call_tool("browser_snapshot", {})

    async def navigate(self, url: str) -> str:
        return await self.call_tool("browser_navigate", {"url": url})

    async def save_storage_state(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        return await self.call_tool("browser_storage_state", {"filename": str(path)})

    async def restore_storage_state(self, path: Path) -> str:
        return await self.call_tool("browser_set_storage_state", {"filename": str(path)})

    async def screenshot_data_url(self) -> str | None:
        """JPEG data URL for the UI pane. Best-effort; never raises to the loop."""
        if self.client is None:
            return None
        frame_path = self.output_dir / "ui-frame.jpg"
        try:
            result = await self.client.call_tool(
                "browser_take_screenshot",
                {"type": "jpeg", "filename": str(frame_path)},
            )
        except Exception:  # noqa: BLE001
            return None
        content = getattr(result, "content", None) or []
        for item in content:
            kind = getattr(item, "type", None)
            data = getattr(item, "data", None)
            mime = getattr(item, "mimeType", None) or "image/jpeg"
            if kind == "image" and data:
                return f"data:{mime};base64,{data}"
        if frame_path.is_file():
            raw = frame_path.read_bytes()
            if raw:
                return "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")
        return None

    async def click_xy(self, x: int, y: int) -> str:
        return await self.call_tool("browser_mouse_click_xy", {"x": int(x), "y": int(y)})

    async def press_key(self, key: str) -> str:
        return await self.call_tool("browser_press_key", {"key": key})
