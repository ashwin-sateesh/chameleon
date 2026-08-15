"""Orchestrator: plan → navigate → guardian → persist. Owns MCP execution and resume."""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from typing import Any

from chameleon.agents.guardian import guardian_check
from chameleon.agents.navigator import navigator_step
from chameleon.agents.planner import planner_plan
from chameleon.mcp_client import PlaywrightMCP, extract_url, normalize_tool_arguments
from chameleon.narration import answer as narrate_answer
from chameleon.narration import ask as narrate_ask
from chameleon.narration import say
from chameleon.paths import session_dir, storage_state_path
from chameleon.profiles import SiteProfile, load_profile
from chameleon.state import (
    ActionRecord,
    GuardianAnswer,
    TaskState,
    TaskStatus,
    load_state,
    new_state,
    save_state,
)

MAX_ACTIONS_TOTAL = 80
MAX_ACTIONS_PER_SUBGOAL = 12


class StopRequested(Exception):
    """SIGINT / cooperative shutdown."""


async def _ask_user(question: str) -> str:
    narrate_ask(question)
    try:
        text = await asyncio.to_thread(input, "> ")
    except EOFError as exc:
        raise StopRequested from exc
    text = text.strip()
    narrate_answer(text)
    return text


async def _hold_browser_open() -> None:
    """Keep the headed browser up until the user dismisses it."""
    if os.environ.get("PYTEST_CURRENT_TEST") or not sys.stdin.isatty():
        return
    say("system", "Browser left open. Press Enter (or close the window) when you are done.")
    try:
        await asyncio.to_thread(input, "")
    except (EOFError, KeyboardInterrupt):
        pass


async def _persist_browser(mcp: PlaywrightMCP, state: TaskState, snapshot: str) -> None:
    url = extract_url(snapshot)
    if url:
        state.current_url = url
    dump = storage_state_path(state.task_id)
    try:
        await mcp.save_storage_state(dump)
        state.storage_state_path = str(dump)
    except Exception as exc:  # noqa: BLE001 — persistence must not kill the loop
        say("system", f"storage_state save skipped: {exc}")
    save_state(state)


async def _restore_browser(mcp: PlaywrightMCP, state: TaskState, profile: SiteProfile) -> str:
    dump = storage_state_path(state.task_id)
    if dump.is_file() and dump.stat().st_size > 2:
        try:
            await mcp.restore_storage_state(dump)
            say("system", f"Restored storage_state from {dump}")
        except Exception as exc:  # noqa: BLE001
            say("system", f"storage_state restore skipped: {exc}")
    target = state.current_url or profile.base_url
    say("system", f"Opening {target}")
    await mcp.navigate(target)
    snapshot = await mcp.snapshot()
    url = extract_url(snapshot)
    if url:
        state.current_url = url
        save_state(state)
    return snapshot


async def _handle_pending_question(state: TaskState) -> None:
    if state.status != TaskStatus.paused_ask or not state.pending_question:
        return
    already = {a.question for a in state.guardian_answers}
    if state.pending_question in already:
        state.pending_question = None
        state.status = TaskStatus.running
        save_state(state)
        return
    reply = await _ask_user(state.pending_question)
    state.guardian_answers.append(
        GuardianAnswer(
            subgoal_index=state.current_subgoal_index,
            question=state.pending_question,
            answer=reply,
        )
    )
    state.pending_question = None
    state.status = TaskStatus.running
    save_state(state)


def _advance_subgoal(state: TaskState) -> None:
    state.current_subgoal_index += 1
    if state.current_subgoal_index >= len(state.planner_checklist):
        state.status = TaskStatus.completed
        say("planner", "Checklist complete.")
    else:
        nxt = state.planner_checklist[state.current_subgoal_index]
        say("planner", f"Next sub-goal: [{nxt.risk}] {nxt.goal}")
    save_state(state)


async def run_task(site: str, task: str, task_id: str) -> TaskState:
    profile: SiteProfile = load_profile(site)
    existing = load_state(task_id)
    resuming = existing is not None
    if resuming:
        state = existing
        if task and task != state.task:
            say("system", f"Resume ignores new --task text; using stored task: {state.task}")
        say("system", f"Resuming task {task_id} at sub-goal {state.current_subgoal_index}")
        if state.status == TaskStatus.completed:
            say("system", "Task already completed.")
            return state
        if state.status == TaskStatus.failed:
            say("system", "Previous run failed; continuing from saved index.")
            state.status = TaskStatus.running
            save_state(state)
    else:
        checklist = planner_plan(task, profile)
        state = new_state(
            site=profile.id,
            task=task,
            task_id=task_id,
            checklist=checklist,
            current_url=profile.base_url,
        )
        save_state(state)

    stop = False

    def _request_stop() -> None:
        nonlocal stop
        stop = True
        say("system", "Stop requested — persisting state.")

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _request_stop)
        except NotImplementedError:
            signal.signal(sig, lambda *_: _request_stop())

    sess = session_dir(task_id)
    actions_total = 0
    actions_subgoal = 0
    last_subgoal = state.current_subgoal_index

    try:
        async with PlaywrightMCP(user_data_dir=sess, output_dir=sess) as mcp:
            try:
                snapshot = await _restore_browser(mcp, state, profile)
                if not resuming:
                    say("system", "Fresh session — Navigator will start from the profile base URL.")
                await _handle_pending_question(state)

                while state.status in {TaskStatus.running, TaskStatus.paused_ask}:
                    if stop:
                        raise StopRequested
                    if state.current_subgoal_index >= len(state.planner_checklist):
                        state.status = TaskStatus.completed
                        save_state(state)
                        break
                    if state.current_subgoal_index != last_subgoal:
                        actions_subgoal = 0
                        last_subgoal = state.current_subgoal_index

                    subgoal = state.planner_checklist[state.current_subgoal_index]
                    history = [record.model_dump() for record in state.navigator_action_history]
                    try:
                        proposal = navigator_step(
                            subgoal=subgoal,
                            snapshot=snapshot,
                            history=history,
                            guardian_answers=state.guardian_answers,
                            profile=profile,
                            task=state.task,
                            subgoal_index=state.current_subgoal_index,
                            total_subgoals=len(state.planner_checklist),
                        )
                    except (ValueError, KeyError) as exc:
                        say("system", f"Navigator output unreadable ({exc}); taking a fresh snapshot.")
                        snapshot = await mcp.snapshot()
                        actions_subgoal += 1
                        continue

                    if proposal.tool:
                        action: dict[str, Any] = {
                            "tool": proposal.tool,
                            "arguments": proposal.arguments,
                            "reason": proposal.reason,
                        }
                        if subgoal.risk != "none":
                            verdict = guardian_check(
                                proposed_action=action,
                                subgoal=subgoal,
                                guardian_answers=state.guardian_answers,
                                task=state.task,
                            )
                            if verdict.decision == "ASK":
                                state.status = TaskStatus.paused_ask
                                state.pending_question = verdict.question
                                save_state(state)
                                reply = await _ask_user(verdict.question or "")
                                state.guardian_answers.append(
                                    GuardianAnswer(
                                        subgoal_index=state.current_subgoal_index,
                                        question=verdict.question or "",
                                        answer=reply,
                                    )
                                )
                                state.pending_question = None
                                state.status = TaskStatus.running
                                save_state(state)
                                continue
                        try:
                            arguments = normalize_tool_arguments(proposal.tool, proposal.arguments)
                        except ValueError as exc:
                            say("system", str(exc))
                            snapshot = await mcp.snapshot()
                            continue
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
                        actions_total += 1
                        actions_subgoal += 1
                        snapshot = await mcp.snapshot()
                        await _persist_browser(mcp, state, snapshot)

                    if proposal.subgoal_complete:
                        _advance_subgoal(state)
                        snapshot = await mcp.snapshot()
                        await _persist_browser(mcp, state, snapshot)
                        continue

                    if not proposal.tool and not proposal.subgoal_complete:
                        say("system", "Navigator proposed no action; taking a fresh snapshot.")
                        snapshot = await mcp.snapshot()
                        actions_subgoal += 1

                    if actions_subgoal >= MAX_ACTIONS_PER_SUBGOAL:
                        say("system", "Sub-goal action limit reached; advancing.")
                        _advance_subgoal(state)
                    if actions_total >= MAX_ACTIONS_TOTAL:
                        state.status = TaskStatus.failed
                        state.error = "Exceeded maximum actions"
                        save_state(state)
                        say("system", state.error)
                        break
            finally:
                await _hold_browser_open()

    except StopRequested:
        if state.status == TaskStatus.running:
            state.status = TaskStatus.paused_ask if state.pending_question else TaskStatus.running
        save_state(state)
        say("system", f"Saved {task_id} ({state.status.value}). Re-run with the same --task-id to resume.")
    except Exception as exc:
        state.status = TaskStatus.failed
        state.error = str(exc)
        save_state(state)
        raise
    else:
        save_state(state)
        if state.status == TaskStatus.completed:
            say("system", "Done.")
    return state
