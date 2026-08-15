"""Color-coded agent handoff logs (terminal + optional UI bridge)."""

from __future__ import annotations

PLANNER = "\033[95m"
NAVIGATOR = "\033[96m"
GUARDIAN = "\033[93m"
ASK = "\033[91m"
ANSWER = "\033[92m"
DIM = "\033[90m"
RESET = "\033[0m"
BOLD = "\033[1m"

_COLORS = {
    "planner": PLANNER,
    "navigator": NAVIGATOR,
    "guardian": GUARDIAN,
    "ask": ASK,
    "answer": ANSWER,
    "system": DIM,
}


def say(agent: str, message: str) -> None:
    color = _COLORS.get(agent.lower(), DIM)
    label = agent.upper()
    print(f"{color}{BOLD}[{label}]{RESET} {color}{message}{RESET}", flush=True)
    from chameleon.ui.events import emit

    emit("log", agent=agent.lower(), message=message)


def ask(question: str) -> None:
    print(f"{ASK}{BOLD}[GUARDIAN ASK]{RESET} {ASK}{question}{RESET}", flush=True)


def answer(text: str) -> None:
    print(f"{ANSWER}{BOLD}[USER]{RESET} {ANSWER}{text}{RESET}", flush=True)
    from chameleon.ui.events import emit

    emit("log", agent="answer", message=text)
