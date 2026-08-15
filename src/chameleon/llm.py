"""LLM wrapper. Claude if CLAUDE_API_KEY/ANTHROPIC_API_KEY is set, else Grok. No MCP."""

from __future__ import annotations

import json
import os
import re
from typing import Any

CLAUDE_MODEL = "claude-sonnet-4-6"
GROK_MODEL = "grok-4.6"


def _claude_key() -> str | None:
    return os.getenv("ANTHROPIC_API_KEY") or os.getenv("CLAUDE_API_KEY")


def _complete_claude(api_key: str, prompt: str, system: str | None) -> str:
    from anthropic import Anthropic

    client = Anthropic(api_key=api_key)
    kwargs: dict[str, Any] = {
        "model": CLAUDE_MODEL,
        "max_tokens": 4096,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        kwargs["system"] = system
    response = client.messages.create(**kwargs)
    parts: list[str] = []
    for block in response.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    return "\n".join(parts)


def _complete_grok(api_key: str, prompt: str, system: str | None) -> str:
    from xai_sdk import Client
    from xai_sdk.chat import system as sys_msg
    from xai_sdk.chat import user as user_msg

    client = Client(api_key=api_key)
    chat = client.chat.create(model=GROK_MODEL)
    if system:
        chat.append(sys_msg(system))
    chat.append(user_msg(prompt))
    response = chat.sample()
    return response.content or ""


def has_llm_key() -> bool:
    return bool(_claude_key() or os.getenv("XAI_API_KEY"))


def complete(prompt: str, *, system: str | None = None) -> str:
    claude = _claude_key()
    if claude:
        return _complete_claude(claude, prompt, system)
    grok = os.getenv("XAI_API_KEY")
    if grok:
        return _complete_grok(grok, prompt, system)
    raise RuntimeError(
        "No LLM key set. Add CLAUDE_API_KEY (or ANTHROPIC_API_KEY) or XAI_API_KEY to .env"
    )


def parse_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", stripped, re.DOTALL)
    if fenced:
        stripped = fenced.group(1)
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start == -1 or end == -1 or end <= start:
        raise ValueError(f"No JSON object in model output:\n{text[:500]}")
    return json.loads(stripped[start : end + 1])
