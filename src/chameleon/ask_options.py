"""Extract clickable ASK replies from a Guardian question."""

from __future__ import annotations

import re

_CHOICE_Q = re.compile(r"\b(which|choose|pick|select|should i|shall i)\b", re.I)
_CONFIRM_Q = re.compile(
    r"\bshould i\b.*\b(submit|place|confirm|order|pay|delete|send)\b"
    r"|\b(place the order|submit (the )?(order|application|form)|confirm (this|the|order|application))\b",
    re.I,
)
_FIELDY = re.compile(
    r"\b(name|email|e-mail|phone|zip|postal|address|resume|password|first|last|dob|birth)\b",
    re.I,
)
_BULLET = re.compile(r"(?m)^\s*(?:[-*]|\d+[.)])\s+(.+?)\s*$")
_QUOTED = re.compile(r'"([^"]{1,80})"')
_OR_SPLIT = re.compile(r"\s*,\s*(?:and\s+|or\s+)?|\s+or\s+", re.I)


def _clean(value: str) -> str:
    text = " ".join(str(value).split()).strip(" \t.:;")
    return text.strip("\"'")


def extract_ask_options(question: str, extra: list[str] | None = None) -> list[str]:
    items: list[str] = []
    seen: set[str] = set()

    def add(value: str) -> None:
        text = _clean(value)
        if not text or len(text) > 80:
            return
        key = text.lower()
        if key in seen:
            return
        seen.add(key)
        items.append(text)

    for value in extra or []:
        add(value)

    text = (question or "").strip()
    if text:
        for match in _BULLET.finditer(text):
            add(match.group(1))
        for match in _QUOTED.finditer(text):
            add(match.group(1))
        if _CHOICE_Q.search(text) and len(items) < 2:
            for part in _or_list(text):
                add(part)
        if _CONFIRM_Q.search(text) and len(items) < 2:
            add("Yes")
            add("No")

    if extra:
        return items[:8]
    return items[:8] if len(items) >= 2 else []


def _or_list(question: str) -> list[str]:
    tail = question.strip().rstrip(" ?")
    if ":" in tail:
        tail = tail.split(":", 1)[1].strip()
    elif "?" in question:
        after = question.split("?", 1)[1].strip().rstrip(" ?")
        if after:
            tail = after
    tail = tail.strip(" ?")
    parts = [_clean(part) for part in _OR_SPLIT.split(tail) if _clean(part)]
    if len(parts) < 2:
        return []
    if sum(1 for part in parts if _FIELDY.search(part)) >= max(2, len(parts) - 1):
        return []
    if any(len(part) < 1 or len(part) > 60 for part in parts):
        return []
    return parts
