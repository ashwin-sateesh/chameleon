"""Playwright MCP stdio client. Orchestrator-owned; agents do not import this except Navigator proposals."""

from __future__ import annotations

import json
import re
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any

from mcp import Client, StdioServerParameters, stdio_client
from mcp.types import TextContent

NAVIGATOR_TOOLS = frozenset(
    {
        "browser_navigate",
        "browser_click",
        "browser_type",
        "browser_snapshot",
    }
)

_URL_PATTERNS = [
    re.compile(r"Page URL:\s*(\S+)"),
    re.compile(r"\(current\)[^\n]*\((https?://[^)]+)\)"),
    re.compile(r"^-+\s*url:\s*(\S+)", re.MULTILINE | re.IGNORECASE),
    re.compile(r"URL:\s*(https?://\S+)"),
]


def extract_url(snapshot: str) -> str | None:
    for pattern in _URL_PATTERNS:
        match = pattern.search(snapshot)
        if match:
            return match.group(1).rstrip(".,)")
    return None


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
        if isinstance(item, TextContent) or getattr(item, "type", None) == "text":
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
    def __init__(self, *, user_data_dir: Path, output_dir: Path) -> None:
        self.user_data_dir = user_data_dir
        self.output_dir = output_dir
        self._stack = AsyncExitStack()
        self.client: Client | None = None

    async def __aenter__(self) -> PlaywrightMCP:
        await self.start()
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.close()

    async def start(self) -> None:
        self.user_data_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        params = StdioServerParameters(
            command="npx",
            args=[
                "-y",
                "@playwright/mcp@latest",
                "--user-data-dir",
                str(self.user_data_dir),
                "--output-dir",
                str(self.output_dir),
                "--caps",
                "storage",
                "--viewport-size",
                "1280x720",
            ],
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
