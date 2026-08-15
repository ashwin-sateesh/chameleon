"""Terminal-led copilot: confirm, act, then suggest unused on-page options."""

from __future__ import annotations

import asyncio
import os
import signal
from typing import Any

from chameleon.agents.guardian import guardian_copilot_action, guardian_copilot_interpret
from chameleon.agents.navigator import navigator_copilot_step
from chameleon.agents.planner import planner_observe
from chameleon.fingerprint import (
    blocker_question,
    detect_blocker,
    is_end_phrase,
    page_fingerprint,
)
from chameleon.loop import StopRequested, _persist_browser, _restore_browser
from chameleon.mcp_client import (
    PlaywrightMCP,
    extract_url,
    looks_like_snapshot,
    normalize_tool_arguments,
    parse_browser_tabs,
)
from chameleon.narration import answer as narrate_answer
from chameleon.narration import ask as narrate_ask
from chameleon.narration import say
from chameleon.paths import session_dir
from chameleon.profiles import SiteProfile
from chameleon.state import (
    ActionRecord,
    CopilotPhase,
    GuardianAnswer,
    ObservedEvent,
    TaskState,
    TaskStatus,
    load_state,
    new_state,
    save_state,
)

MAX_ACT_ACTIONS = 24
MAX_OBSERVED_EVENTS = 20
PROCEED_NUDGE = (
    "Click something on the page, type what you want next, or type done."
)


def followup_question(*, unmet: list[str], extras: list[str]) -> str | None:
    missing = [item for item in unmet if item][:2]
    shown = [item for item in extras if item][:2]
    if missing:
        label = " and ".join(missing)
        if shown:
            return (
                f"{label} isn't on this page. I can do {' or '.join(shown)} instead. "
                "Skip it / which / done?"
            )
        return f"{label} isn't on this page. Skip it, tell me another way, or type done."
    if len(shown) == 1:
        return f"I can {shown[0]}. OK / done?"
    if len(shown) >= 2:
        return f"I can {shown[0]} or {shown[1]}. Which / done?"
    return None


def _poll_seconds() -> float:
    return float(os.getenv("CHAMELEON_COPILOT_POLL", "1"))


def _debounce_polls() -> int:
    return max(1, int(os.getenv("CHAMELEON_COPILOT_DEBOUNCE", "2")))


async def read_user_line() -> str:
    try:
        return await asyncio.to_thread(input, "> ")
    except EOFError:
        return "done"


def task_is_specific(profile: SiteProfile, task: str) -> bool:
    text = (task or "").strip().lower()
    if not text:
        return False
    default = (profile.default_task or "").strip().lower()
    if text == default:
        return False
    return not text.startswith("accompany the user")


def _complete(state: TaskState) -> None:
    state.status = TaskStatus.completed
    state.phase = CopilotPhase.observing
    state.pending_question = None
    state.consented_goal = None
    save_state(state)
    say("system", "Done.")


def _enter_ask(state: TaskState, question: str, *, proposed_goal: str | None = None) -> None:
    state.phase = CopilotPhase.asking
    state.status = TaskStatus.paused_ask
    state.pending_question = question
    if proposed_goal:
        state.consented_goal = proposed_goal
    save_state(state)
    narrate_ask(question)


def _append_event(state: TaskState, summary: str) -> None:
    state.observed_events.append(ObservedEvent(url=state.current_url, summary=summary))
    if len(state.observed_events) > MAX_OBSERVED_EVENTS:
        state.observed_events = state.observed_events[-MAX_OBSERVED_EVENTS:]


def _mark_page_known(state: TaskState, snapshot: str) -> None:
    fp = page_fingerprint(snapshot, state.current_url)
    state.last_fingerprint = fp
    state.interpreted_fingerprint = fp
    save_state(state)


def _already_asked(state: TaskState, question: str) -> bool:
    return any(a.question == question for a in state.guardian_answers) or state.pending_question == question


async def run_copilot(profile: SiteProfile, task: str, task_id: str) -> TaskState:
    existing = load_state(task_id)
    resuming = existing is not None
    if resuming:
        state = existing
        if task and task != state.task:
            say("system", f"Resume ignores new --task text; using stored task: {state.task}")
        say("system", f"Resuming copilot {task_id} ({state.phase or CopilotPhase.observing.value})")
        if state.status == TaskStatus.completed:
            say("system", "Task already completed.")
            return state
        if state.status == TaskStatus.failed:
            say("system", "Previous run failed; continuing.")
            state.status = TaskStatus.running
            state.phase = CopilotPhase.observing
            save_state(state)
        elif state.status == TaskStatus.paused_ask and state.pending_question:
            state.phase = CopilotPhase.asking
        elif state.phase is None:
            state.phase = CopilotPhase.observing
    else:
        state = new_state(
            site=profile.id,
            task=task,
            task_id=task_id,
            checklist=[],
            current_url=profile.base_url,
            phase=CopilotPhase.observing,
        )
        save_state(state)
        say("planner", f"Copilot on {profile.name}: {task}")

    stop = False
    stop_event = asyncio.Event()
    actions_this_goal = 0
    latest_snapshot = ""
    mcp: PlaywrightMCP | None = None
    pending_change: str | None = None
    stable_count = 0
    nudged_fingerprint: str | None = None
    last_intents: list[str] = []
    last_tab_count = 0

    def _request_stop() -> None:
        nonlocal stop
        stop = True
        stop_event.set()
        say("system", "Stop requested — persisting state.")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop())

    def _finish_acting(snapshot: str) -> None:
        nonlocal actions_this_goal
        state.phase = CopilotPhase.observing
        state.consented_goal = None
        if state.status != TaskStatus.paused_ask:
            state.status = TaskStatus.running
        _mark_page_known(state, snapshot)
        actions_this_goal = 0

    async def _act_one_step(snapshot: str) -> str:
        nonlocal actions_this_goal
        goal = state.consented_goal
        if not goal:
            state.phase = CopilotPhase.observing
            return snapshot
        if actions_this_goal >= MAX_ACT_ACTIONS:
            say("system", "Stopping this burst — action limit reached.")
            _finish_acting(snapshot)
            return snapshot
        history = [record.model_dump() for record in state.navigator_action_history]
        proposal = await asyncio.to_thread(
            navigator_copilot_step,
            micro_goal=goal,
            snapshot=snapshot,
            history=history,
            profile=profile,
            task=state.task,
        )
        if proposal.tool:
            action: dict[str, Any] = {
                "tool": proposal.tool,
                "arguments": proposal.arguments,
                "reason": proposal.reason,
            }
            verdict = guardian_copilot_action(
                proposed_action=action,
                micro_goal=goal,
                task=state.task,
            )
            if verdict.decision == "ASK":
                _enter_ask(state, verdict.question or "Continue this action?")
                return snapshot
            if verdict.decision != "PROCEED":
                _finish_acting(snapshot)
                return snapshot
            if mcp is None:
                raise RuntimeError("Playwright MCP is not started")
            try:
                arguments = normalize_tool_arguments(proposal.tool, proposal.arguments)
            except ValueError as exc:
                say("system", str(exc))
                return await mcp.snapshot()
            result = await mcp.call_tool(proposal.tool, arguments)
            excerpt = (result or "")[:400]
            state.navigator_action_history.append(
                ActionRecord(
                    tool=proposal.tool,
                    arguments=arguments,
                    reason=proposal.reason,
                    result_excerpt=excerpt,
                )
            )
            actions_this_goal += 1
            snapshot = await mcp.snapshot()
            await _persist_browser(mcp, state, snapshot)
        if proposal.subgoal_complete or not proposal.tool:
            _finish_acting(snapshot)
        return snapshot

    async def _run_burst(snapshot: str) -> str:
        while (
            state.phase == CopilotPhase.acting
            and state.status == TaskStatus.running
            and not stop
            and actions_this_goal < MAX_ACT_ACTIONS
        ):
            snapshot = await _act_one_step(snapshot)
            if state.phase != CopilotPhase.acting:
                break
        if state.phase == CopilotPhase.acting:
            if actions_this_goal >= MAX_ACT_ACTIONS:
                say("system", "Stopping this burst — action limit reached.")
            _finish_acting(snapshot)
        if state.phase == CopilotPhase.observing and not state.pending_question:
            await _suggest_hidden(snapshot)
        return snapshot

    async def _suggest_hidden(snapshot: str, *, replace_question: bool = False) -> None:
        nonlocal pending_change, stable_count, nudged_fingerprint, last_intents
        if state.phase == CopilotPhase.acting:
            return
        if state.pending_question and not replace_question:
            return
        if replace_question:
            state.pending_question = None
            if state.status == TaskStatus.paused_ask:
                state.status = TaskStatus.running
            state.phase = CopilotPhase.observing
        observation = await asyncio.to_thread(
            planner_observe,
            snapshot=snapshot,
            url=state.current_url,
            last_user_state=state.user_state,
            pending_question=None if replace_question else state.pending_question,
            guardian_answers=state.guardian_answers,
            observed_events=state.observed_events,
            profile=profile,
            task=state.task,
            replace_question=replace_question,
        )
        if observation.user_state:
            state.user_state = observation.user_state
            _append_event(state, observation.user_state)
        _mark_page_known(state, snapshot)
        pending_change = None
        stable_count = 0
        if observation.blocker:
            question = blocker_question(observation.blocker)  # type: ignore[arg-type]
            if not _already_asked(state, question):
                _enter_ask(state, question)
                return
        unmet = list(observation.unmet_constraints)
        extras = list(observation.possible_intents)
        last_intents = extras
        if unmet:
            question = followup_question(unmet=unmet, extras=extras)
        elif observation.should_ask and observation.question:
            question = observation.question
        else:
            question = followup_question(unmet=[], extras=extras)
        if question and not _already_asked(state, question):
            goal = observation.suggested_micro_goal
            if not goal and extras:
                goal = extras[0]
            _enter_ask(state, question, proposed_goal=goal)
            return
        fp = state.interpreted_fingerprint
        if fp and fp != nudged_fingerprint:
            nudged_fingerprint = fp
            say("system", PROCEED_NUDGE)
        save_state(state)

    async def _focus_new_tab() -> tuple[bool, str]:
        nonlocal last_tab_count
        if mcp is None:
            return False, ""
        try:
            listing = await mcp.list_tabs()
        except Exception:  # noqa: BLE001
            return False, await mcp.snapshot()
        tabs = parse_browser_tabs(listing)
        count = len(tabs)
        if last_tab_count == 0:
            last_tab_count = max(count, 1)
            return False, await mcp.snapshot()
        if count > last_tab_count:
            last_tab_count = count
            newest = max(tabs, key=lambda tab: tab.index)
            say("system", "New tab opened — switching to it.")
            try:
                selected = await mcp.select_tab(newest.index)
            except Exception as exc:  # noqa: BLE001
                say("system", f"Could not switch tab: {exc}")
                return False, await mcp.snapshot()
            snapshot = selected if looks_like_snapshot(selected) else await mcp.snapshot()
            await _persist_browser(mcp, state, snapshot)
            return True, snapshot
        last_tab_count = count
        return False, await mcp.snapshot()

    async def _on_snapshot(snapshot: str) -> str:
        nonlocal pending_change, stable_count
        url = extract_url(snapshot)
        if url:
            state.current_url = url
        fp = page_fingerprint(snapshot, state.current_url)
        state.last_fingerprint = fp

        blocker = detect_blocker(snapshot)
        if blocker:
            question = blocker_question(blocker)
            if not _already_asked(state, question):
                _enter_ask(state, question)
                return snapshot

        if state.phase == CopilotPhase.acting:
            save_state(state)
            return snapshot

        if fp == state.interpreted_fingerprint:
            pending_change = None
            stable_count = 0
            return snapshot

        if fp != pending_change:
            pending_change = fp
            stable_count = 1
        else:
            stable_count += 1
        if stable_count < _debounce_polls():
            save_state(state)
            return snapshot

        say("planner", "Page changed — reading it.")
        await _suggest_hidden(
            snapshot,
            replace_question=bool(state.pending_question),
        )
        return snapshot

    async def _on_user_text(text: str) -> None:
        nonlocal latest_snapshot
        narrate_answer(text)
        if is_end_phrase(text, profile.end_phrases):
            _complete(state)
            return

        pending = state.pending_question or ""
        lowered = pending.lower()
        if state.phase == CopilotPhase.asking and (
            "cookie" in lowered or "consent" in lowered or "captcha" in lowered or "unusual-traffic" in lowered
        ):
            state.guardian_answers.append(
                GuardianAnswer(
                    subgoal_index=len(state.observed_events),
                    question=state.pending_question or "",
                    answer=text,
                )
            )
            state.pending_question = None
            state.phase = CopilotPhase.observing
            state.status = TaskStatus.running
            save_state(state)
            say("system", "Thanks.")
            return

        verdict = await asyncio.to_thread(
            guardian_copilot_interpret,
            pending_question=state.pending_question,
            user_reply=text,
            user_state=state.user_state,
            possible_intents=last_intents,
            suggested_micro_goal=state.consented_goal or state.task,
            guardian_answers=state.guardian_answers,
            task=state.task,
            site_name=profile.name,
        )
        state.guardian_answers.append(
            GuardianAnswer(
                subgoal_index=len(state.observed_events),
                question=state.pending_question or "(instruction)",
                answer=text,
            )
        )
        state.pending_question = None
        if state.status == TaskStatus.paused_ask:
            state.status = TaskStatus.running

        if verdict.decision == "DONE":
            _complete(state)
            return
        if verdict.decision == "WAIT":
            state.phase = CopilotPhase.observing
            state.consented_goal = None
            save_state(state)
            say("system", PROCEED_NUDGE)
            return
        if verdict.decision == "ASK":
            _enter_ask(
                state,
                verdict.question or "Which option?",
                proposed_goal=verdict.micro_goal or state.consented_goal,
            )
            return

        goal = verdict.micro_goal or state.consented_goal or state.task
        if not goal:
            state.phase = CopilotPhase.observing
            save_state(state)
            say("system", "Tell me what to do.")
            return
        state.phase = CopilotPhase.acting
        state.consented_goal = goal
        state.status = TaskStatus.running
        save_state(state)
        say("navigator", f"Doing: {goal}")
        if latest_snapshot:
            latest_snapshot = await _run_burst(latest_snapshot)

    sess = session_dir(task_id)
    input_task: asyncio.Task[str] | None = None
    try:
        async with PlaywrightMCP(user_data_dir=sess, output_dir=sess) as started:
            mcp = started
            snapshot = await _restore_browser(mcp, state, profile)
            latest_snapshot = snapshot
            await _persist_browser(mcp, state, snapshot)
            fp = page_fingerprint(snapshot, state.current_url)
            state.last_fingerprint = fp
            if not resuming:
                blocker = detect_blocker(snapshot)
                if blocker:
                    _enter_ask(state, blocker_question(blocker))
                else:
                    state.interpreted_fingerprint = fp
                    save_state(state)
                    say(
                        "system",
                        f"{profile.name} is open. Tell me what you want here and I will do it. "
                        "Type done to finish.",
                    )
                    if task_is_specific(profile, state.task):
                        _enter_ask(
                            state,
                            f"I'll {state.task.rstrip('.')}. OK?",
                            proposed_goal=state.task,
                        )
            else:
                if state.interpreted_fingerprint is None and state.phase != CopilotPhase.asking:
                    state.interpreted_fingerprint = fp
                    save_state(state)
                say("system", f"Resumed. Tell me what to do, or type done.")
                if state.pending_question:
                    narrate_ask(state.pending_question)

            input_task = asyncio.create_task(read_user_line())
            while state.status in {TaskStatus.running, TaskStatus.paused_ask}:
                if stop:
                    raise StopRequested
                poll_task = asyncio.create_task(asyncio.sleep(_poll_seconds()))
                stop_task = asyncio.create_task(stop_event.wait())
                done, _ = await asyncio.wait(
                    {input_task, poll_task, stop_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for extra in (poll_task, stop_task):
                    if extra not in done:
                        extra.cancel()
                        try:
                            await extra
                        except asyncio.CancelledError:
                            pass
                if stop or stop_task in done:
                    raise StopRequested
                if input_task in done:
                    try:
                        text = input_task.result()
                    except StopRequested:
                        raise
                    except asyncio.CancelledError:
                        raise
                    except Exception:  # noqa: BLE001
                        text = "done"
                    input_task = asyncio.create_task(read_user_line())
                    text = (text or "").strip()
                    if text:
                        await _on_user_text(text)
                    continue
                switched, snapshot = await _focus_new_tab()
                if switched:
                    say("planner", "New tab — reading it.")
                    await _suggest_hidden(
                        snapshot,
                        replace_question=True,
                    )
                    latest_snapshot = snapshot
                    continue
                latest_snapshot = await _on_snapshot(snapshot)
    except StopRequested:
        if state.status == TaskStatus.running:
            state.status = TaskStatus.paused_ask if state.pending_question else TaskStatus.running
        save_state(state)
        say(
            "system",
            f"Saved {task_id} ({state.status.value}). Re-run with the same --task-id to resume.",
        )
    except Exception as exc:
        state.status = TaskStatus.failed
        state.error = str(exc)
        save_state(state)
        raise
    else:
        save_state(state)
        if state.status == TaskStatus.completed:
            say("system", "Copilot session finished.")
    finally:
        if input_task is not None and not input_task.done():
            input_task.cancel()
            try:
                await input_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
    return state
