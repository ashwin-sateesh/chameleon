"""Copilot orchestrator: observe the headed page, ask in the terminal, act only after consent."""

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
from chameleon.mcp_client import PlaywrightMCP, extract_url, normalize_tool_arguments
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

MAX_ACT_ACTIONS = 12
MAX_OBSERVED_EVENTS = 20


def _poll_seconds() -> float:
    return float(os.getenv("CHAMELEON_COPILOT_POLL", "3"))


def _debounce_polls() -> int:
    return max(1, int(os.getenv("CHAMELEON_COPILOT_DEBOUNCE", "2")))


async def read_user_line() -> str:
    try:
        return await asyncio.to_thread(input, "> ")
    except EOFError:
        return "done"


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
            say("system", "Previous run failed; continuing in observe mode.")
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
        say("planner", "I will not click or type until you ask me to.")

    stop = False
    stop_event = asyncio.Event()
    pending_change: str | None = None
    stable_count = 0
    actions_this_goal = 0
    latest_snapshot = ""
    mcp: PlaywrightMCP | None = None

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
        nonlocal actions_this_goal, pending_change, stable_count
        state.phase = CopilotPhase.observing
        state.consented_goal = None
        if state.status != TaskStatus.paused_ask:
            state.status = TaskStatus.running
        _mark_page_known(state, snapshot)
        actions_this_goal = 0
        pending_change = None
        stable_count = 0
        say("system", "Back to watching. Search, click, or tell me what to do.")

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
            verdict = await asyncio.to_thread(
                guardian_copilot_action,
                proposed_action=action,
                micro_goal=goal,
                task=state.task,
            )
            if verdict.decision == "ASK":
                _enter_ask(state, verdict.question or "Should I continue this action?")
                return snapshot
            if verdict.decision != "PROCEED":
                say("guardian", "Skipping this action; you drive.")
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

    async def _interpret(snapshot: str) -> None:
        nonlocal pending_change, stable_count
        if state.phase == CopilotPhase.asking and state.pending_question:
            return
        observation = await asyncio.to_thread(
            planner_observe,
            snapshot=snapshot,
            url=state.current_url,
            last_user_state=state.user_state,
            pending_question=state.pending_question,
            guardian_answers=state.guardian_answers,
            observed_events=state.observed_events,
            profile=profile,
            task=state.task,
        )
        if observation.user_state:
            state.user_state = observation.user_state
            _append_event(state, observation.user_state)
        fp = page_fingerprint(snapshot, state.current_url)
        state.last_fingerprint = fp
        state.interpreted_fingerprint = fp
        pending_change = None
        stable_count = 0
        if observation.blocker:
            question = blocker_question(observation.blocker)  # type: ignore[arg-type]
            if not _already_asked(state, question):
                _enter_ask(state, question)
                return
        if observation.should_ask and observation.question:
            if not _already_asked(state, observation.question):
                _enter_ask(
                    state,
                    observation.question,
                    proposed_goal=observation.suggested_micro_goal,
                )
                return
        save_state(state)

    async def _on_snapshot(snapshot: str) -> str:
        nonlocal pending_change, stable_count
        url = extract_url(snapshot)
        if url:
            state.current_url = url
        fp = page_fingerprint(snapshot, state.current_url)
        state.last_fingerprint = fp

        if state.phase == CopilotPhase.acting:
            return await _act_one_step(snapshot)

        blocker = detect_blocker(snapshot)
        if blocker:
            question = blocker_question(blocker)
            if not _already_asked(state, question):
                _enter_ask(state, question)
                return snapshot

        if state.phase == CopilotPhase.asking:
            save_state(state)
            return snapshot

        if fp == state.interpreted_fingerprint:
            pending_change = None
            stable_count = 0
            return snapshot

        if fp != pending_change:
            pending_change = fp
            stable_count = 1
            save_state(state)
            say("system", "Page changed — waiting until it settles.")
        else:
            stable_count += 1
        if stable_count < _debounce_polls():
            return snapshot

        await _interpret(snapshot)
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
            state.interpreted_fingerprint = None
            save_state(state)
            say("system", "Thanks. I'll keep watching.")
            return

        verdict = await asyncio.to_thread(
            guardian_copilot_interpret,
            pending_question=state.pending_question,
            user_reply=text,
            user_state=state.user_state,
            possible_intents=[],
            suggested_micro_goal=state.consented_goal,
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
            say("guardian", "You drive — I'll watch.")
            return
        if verdict.decision == "ASK":
            _enter_ask(
                state,
                verdict.question or "Should I do that, or will you?",
                proposed_goal=verdict.micro_goal or state.consented_goal,
            )
            return

        goal = verdict.micro_goal or state.consented_goal
        if not goal:
            state.phase = CopilotPhase.observing
            save_state(state)
            say("guardian", "I need a clearer goal before acting. You can keep exploring.")
            return
        state.phase = CopilotPhase.acting
        state.consented_goal = goal
        state.status = TaskStatus.running
        save_state(state)
        say("navigator", f"Acting on: {goal}")
        if latest_snapshot:
            latest_snapshot = await _act_one_step(latest_snapshot)

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
                        f"{profile.name} is open. Search a place in the browser, click around, "
                        "or type here what you want. Type done when you are finished.",
                    )
            else:
                if state.interpreted_fingerprint is None and state.phase != CopilotPhase.asking:
                    state.interpreted_fingerprint = fp
                    save_state(state)
                say(
                    "system",
                    "Resumed. Keep using the browser, or type here. Type done when you are finished.",
                )
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
                snapshot = await mcp.snapshot()
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
