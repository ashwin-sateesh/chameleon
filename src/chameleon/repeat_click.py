"""Stop Navigator from toggling the same control (e.g. Sauce Demo Add to cart)."""

from __future__ import annotations

import re
from typing import Any

from chameleon.profiles import ChecklistItem

_ADD_CART = re.compile(r"add to cart", re.I)
_REMOVE = re.compile(r"\bremove\b", re.I)
_CART_GOAL = re.compile(r"\b(cart|add the chosen|add the item)\b", re.I)


def _norm(value: Any) -> str:
    return " ".join(str(value or "").lower().split())


def _click_text(arguments: dict[str, Any], reason: str = "") -> str:
    return f"{arguments.get('element', '')} {reason}"


def is_repeat_click(
    tool: str,
    arguments: dict[str, Any],
    reason: str,
    history: list[dict[str, Any]],
    subgoal: ChecklistItem | None = None,
) -> bool:
    if tool != "browser_click" or not history:
        return False
    last = history[-1]
    if last.get("tool") != "browser_click":
        return False
    last_args = last.get("arguments") or {}
    target = str(arguments.get("target") or "")
    last_target = str(last_args.get("target") or "")
    if target and last_target and target == last_target:
        return True
    element = _norm(arguments.get("element"))
    last_element = _norm(last_args.get("element"))
    if element and last_element and element == last_element:
        return True
    cur = _click_text(arguments, reason)
    prev = _click_text(last_args, str(last.get("reason") or ""))
    if _ADD_CART.search(prev) and (_ADD_CART.search(cur) or _REMOVE.search(cur)):
        if subgoal is None or _CART_GOAL.search(subgoal.goal):
            return True
    return False


def complete_after_repeat_click(
    subgoal: ChecklistItem,
    arguments: dict[str, Any],
    reason: str,
) -> bool:
    blob = f"{subgoal.goal} {_click_text(arguments, reason)}"
    return bool(_CART_GOAL.search(subgoal.goal) or _ADD_CART.search(blob) or _REMOVE.search(blob))
