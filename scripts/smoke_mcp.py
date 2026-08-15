#!/usr/bin/env python3
"""Headed Playwright MCP smoke: log into Sauce Demo. No agents."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chameleon.mcp_client import PlaywrightMCP, find_ref  # noqa: E402
from chameleon.paths import session_dir  # noqa: E402

SAUCE = "https://www.saucedemo.com/"


async def main() -> None:
    sess = session_dir("_smoke")
    print("Starting headed Playwright MCP…", flush=True)
    async with PlaywrightMCP(user_data_dir=sess, output_dir=sess) as mcp:
        print(f"Navigating to {SAUCE}", flush=True)
        await mcp.navigate(SAUCE)
        snapshot = await mcp.snapshot()
        print(snapshot[:2000], flush=True)
        user_ref = find_ref(snapshot, "Username")
        pass_ref = find_ref(snapshot, "Password")
        login_ref = find_ref(snapshot, "Login")
        if not (user_ref and pass_ref and login_ref):
            raise SystemExit(
                f"Could not find login controls in snapshot "
                f"(user={user_ref} pass={pass_ref} login={login_ref})"
            )
        print(f"Typing credentials (user={user_ref} pass={pass_ref})", flush=True)
        await mcp.call_tool(
            "browser_type",
            {"element": "Username", "target": user_ref, "text": "standard_user"},
        )
        await mcp.call_tool(
            "browser_type",
            {"element": "Password", "target": pass_ref, "text": "secret_sauce"},
        )
        print(f"Clicking Login ({login_ref})", flush=True)
        await mcp.call_tool("browser_click", {"element": "Login", "target": login_ref})
        after = await mcp.snapshot()
        print(after[:2000], flush=True)
        if "inventory" not in after.lower() and "add to cart" not in after.lower():
            raise SystemExit("Login did not reach inventory — smoke failed")
        print("SMOKE OK: logged into Sauce Demo inventory.", flush=True)


if __name__ == "__main__":
    asyncio.run(main())
