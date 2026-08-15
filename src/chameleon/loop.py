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
from chameleon.ask_options import extract_ask_options
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
from chameleon.ui.events import UiBridge, current_bridge, emit

MAX_ACTIONS_TOTAL = 80
MAX_ACTIONS_PER_SUBGOAL = 12


class StopRequested(Exception):
    """SIGINT / cooperative shutdown."""


class MissingTaskError(ValueError):
    """Raised when an execute site is launched without --task."""


async def _ask_user(question: str, options: list[str] | None = None) -> str:
    narrate_ask(question)
    bridge = current_bridge.get()
    if bridge is not None:
        text = (await bridge.wait_answer(question, options or [])).strip()
        if bridge.stop:
            raise StopRequested
        narrate_answer(text)
        return text
    try:
        text = await asyncio.to_thread(input, "> ")
    except EOFError as exc:
        raise StopRequested from exc
    text = text.strip()
    narrate_answer(text)
    return text


async def _hold_browser_open() -> None:
    """Keep the browser/UI up until the user dismisses it."""
    bridge = current_bridge.get()
    if bridge is not None:
        say("system", "I'm done. Send another message when you want to do something else.")
        await bridge.wait_dismiss()
        return
    if os.environ.get("PYTEST_CURRENT_TEST") or not sys.stdin.isatty():
        return
    say("system", "Browser left open. Press Enter (or close the window) when you are done.")
    try:
        await asyncio.to_thread(input, "")
    except (EOFError, KeyboardInterrupt):
        pass


def _emit_status(state: TaskState) -> None:
    current = None
    if 0 <= state.current_subgoal_index < len(state.planner_checklist):
        item = state.planner_checklist[state.current_subgoal_index]
        current = {"goal": item.goal, "risk": item.risk, "index": state.current_subgoal_index}
    emit(
        "status",
        site=state.site,
        task=state.task,
        task_id=state.task_id,
        status=state.status.value,
        current_url=state.current_url,
        current_subgoal=current,
        checklist=[item.model_dump() for item in state.planner_checklist],
        pending_question=state.pending_question,
    )


async def _emit_frame(mcp: Any) -> None:
    bridge = current_bridge.get()
    if bridge is None or getattr(bridge, "live", None) is not None:
        return
    take = getattr(mcp, "screenshot_data_url", None)
    if take is None:
        return
    try:
        src = await take()
    except Exception:  # noqa: BLE001
        return
    if src:
        emit("frame", src=src)


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
    bridge = current_bridge.get()
    live = getattr(bridge, "live", None) if bridge is not None else None
    if live is not None:
        try:
            captured = await live.dump_page_state()
            if captured:
                state.page_state = captured
                if captured.get("url"):
                    state.current_url = str(captured["url"])
        except Exception as exc:  # noqa: BLE001
            say("system", f"page fill save skipped: {exc}")
    save_state(state)
    _emit_status(state)
    await _emit_frame(mcp)


async def _restore_browser(mcp: PlaywrightMCP, state: TaskState, profile: SiteProfile) -> str:
    dump = storage_state_path(state.task_id)
    if dump.is_file() and dump.stat().st_size > 2:
        try:
            await mcp.restore_storage_state(dump)
            say("system", f"Restored storage_state from {dump}")
        except Exception as exc:  # noqa: BLE001
            say("system", f"storage_state restore skipped: {exc}")
    target = state.current_url or profile.base_url
    if state.page_state and state.page_state.get("url"):
        target = str(state.page_state["url"])
    say("system", f"Opening {target}")
    await mcp.navigate(target)
    bridge = current_bridge.get()
    live = getattr(bridge, "live", None) if bridge is not None else None
    if live is not None:
        try:
            for _ in range(20):
                ready = await live.evaluate("document.readyState")
                if ready == "complete":
                    break
                await asyncio.sleep(0.15)
        except Exception:  # noqa: BLE001
            await asyncio.sleep(0.6)
    else:
        if not os.environ.get("PYTEST_CURRENT_TEST"):
            await asyncio.sleep(0.6)
    if live is not None and state.page_state:
        try:
            await live.restore_page_state(state.page_state)
            await asyncio.sleep(0.35)
            await live.restore_page_state(state.page_state)
            say("system", "Restored filled fields from the last session.")
        except Exception as exc:  # noqa: BLE001
            say("system", f"page fill restore skipped: {exc}")
    snapshot = await mcp.snapshot()
    url = extract_url(snapshot)
    if url:
        state.current_url = url
        save_state(state)
    await _emit_frame(mcp)
    _emit_status(state)
    return snapshot


async def _pause_and_ask(state: TaskState, question: str, options: list[str] | None = None) -> None:
    bridge = current_bridge.get()
    live = getattr(bridge, "live", None) if bridge is not None else None
    if live is not None:
        try:
            captured = await live.dump_page_state()
            if captured:
                state.page_state = captured
                if captured.get("url"):
                    state.current_url = str(captured["url"])
        except Exception as exc:  # noqa: BLE001
            say("system", f"page fill save skipped: {exc}")
    choices = extract_ask_options(question, options)
    state.status = TaskStatus.paused_ask
    state.pending_question = question
    state.pending_options = choices
    save_state(state)
    _emit_status(state)
    reply = await _ask_user(question, choices)
    state.guardian_answers.append(
        GuardianAnswer(
            subgoal_index=state.current_subgoal_index,
            question=question,
            answer=reply,
        )
    )
    state.pending_question = None
    state.pending_options = []
    state.status = TaskStatus.running
    save_state(state)
    _emit_status(state)


async def _handle_pending_question(state: TaskState) -> None:
    if state.status != TaskStatus.paused_ask or not state.pending_question:
        return
    already = {a.question for a in state.guardian_answers}
    if state.pending_question in already:
        state.pending_question = None
        state.pending_options = []
        state.status = TaskStatus.running
        save_state(state)
        return
    await _pause_and_ask(state, state.pending_question, state.pending_options)


async def _guardian_gate(state: TaskState, action: dict[str, Any], subgoal) -> bool:
    """Run Guardian on a risky step. True = caller should retry the turn (asked the user)."""
    if subgoal.risk == "none":
        return False
    verdict = guardian_check(
        proposed_action=action,
        subgoal=subgoal,
        guardian_answers=state.guardian_answers,
        task=state.task,
    )
    if verdict.decision != "ASK":
        return False
    await _pause_and_ask(state, verdict.question or "", verdict.options)
    return True


def _advance_subgoal(state: TaskState) -> None:
    state.current_subgoal_index += 1
    if state.current_subgoal_index >= len(state.planner_checklist):
        state.status = TaskStatus.completed
        say("planner", "Checklist complete.")
    else:
        nxt = state.planner_checklist[state.current_subgoal_index]
        say("planner", f"Next sub-goal: [{nxt.risk}] {nxt.goal}")
    save_state(state)
    _emit_status(state)


async def run_task(
    site: str,
    task: str | None,
    task_id: str,
    *,
    headless: bool = False,
    bridge: UiBridge | None = None,
) -> TaskState:
    token = current_bridge.set(bridge)
    try:
        return await _run_task_inner(site, task, task_id, headless=headless)
    finally:
        current_bridge.reset(token)


async def _run_task_inner(
    site: str,
    task: str | None,
    task_id: str,
    *,
    headless: bool,
) -> TaskState:
    profile: SiteProfile = load_profile(site)
    resolved = (task or "").strip()
    if not resolved:
        if profile.interaction_mode == "copilot":
            resolved = profile.default_task or f"accompany the user on {profile.name}"
        else:
            raise MissingTaskError(
                f"--task is required for execute site {profile.id!r}. "
                "Copilot sites (interaction_mode: copilot) may omit it."
            )
    if profile.interaction_mode == "copilot":
        from chameleon.copilot_loop import run_copilot

        return await run_copilot(profile, resolved, task_id, headless=headless)
    return await _run_execute(profile, resolved, task_id, headless=headless)


async def _run_execute(
    profile: SiteProfile, task: str, task_id: str, *, headless: bool = False
) -> TaskState:
    existing = load_state(task_id)
    resuming = existing is not None
    if resuming:
        state = existing
        if task and task != state.task:
            say("system", f"Resume ignores new --task text; using stored task: {state.task}")
        say("system", f"Resuming task {task_id} at sub-goal {state.current_subgoal_index}")
        if state.status == TaskStatus.completed:
            say("system", "Task already completed.")
            emit("status", site=state.site, task=state.task, task_id=state.task_id, status=state.status.value)
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
    _emit_status(state)

    stop = False
    bridge = current_bridge.get()

    def _request_stop() -> None:
        nonlocal stop
        stop = True
        say("system", "Stop requested — persisting state.")

    if bridge is None:
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
    live = None

    try:
        cdp_endpoint = None
        mcp_headless = headless
        if bridge is not None:
            from chameleon.ui.live_browser import LiveBrowser

            live = LiveBrowser(user_data_dir=sess, emit=bridge.emit)
            await live.start()
            bridge.live = live
            cdp_endpoint = live.endpoint
            mcp_headless = False
            say("system", "Live Chromium attached — streaming into the console.")
        async with PlaywrightMCP(
            user_data_dir=sess,
            output_dir=sess,
            headless=mcp_headless,
            cdp_endpoint=cdp_endpoint,
        ) as mcp:
            if bridge is not None:
                bridge.mcp = mcp
            try:
                snapshot = await _restore_browser(mcp, state, profile)
                if not resuming:
                    say("system", "Fresh session — Navigator will start from the profile base URL.")
                await _handle_pending_question(state)

                while state.status in {TaskStatus.running, TaskStatus.paused_ask}:
                    if stop or (bridge is not None and bridge.stop):
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
                        if await _guardian_gate(state, action, subgoal):
                            await _persist_browser(mcp, state, snapshot)
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
                        blocked = {
                            "tool": None,
                            "arguments": {},
                            "reason": proposal.reason or "Navigator cannot proceed",
                        }
                        if await _guardian_gate(state, blocked, subgoal):
                            await _persist_browser(mcp, state, snapshot)
                            continue
                        has_answers = any(
                            a.subgoal_index == state.current_subgoal_index
                            for a in state.guardian_answers
                        )
                        if subgoal.risk in {"needs_user_info", "ambiguous_choice"} and not has_answers:
                            await _pause_and_ask(
                                state,
                                proposal.reason
                                or "What details should I use for this step?",
                            )
                            continue
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
            except StopRequested:
                try:
                    snap = await mcp.snapshot()
                    await _persist_browser(mcp, state, snap)
                except Exception as exc:  # noqa: BLE001
                    say("system", f"Pause persist skipped: {exc}")
                    save_state(state)
                raise
            else:
                await _hold_browser_open()
            finally:
                if bridge is not None:
                    bridge.mcp = None

    except StopRequested:
        if state.status == TaskStatus.running:
            state.status = TaskStatus.paused_ask if state.pending_question else TaskStatus.running
        save_state(state)
        _emit_status(state)
        say("system", f"Saved {task_id} ({state.status.value}). Re-run with the same --task-id to resume.")
    except Exception as exc:
        state.status = TaskStatus.failed
        state.error = str(exc)
        save_state(state)
        _emit_status(state)
        emit("error", message=str(exc))
        raise
    else:
        save_state(state)
        _emit_status(state)
        if state.status == TaskStatus.completed:
            say("system", "Done.")
    finally:
        if live is not None:
            if bridge is not None:
                bridge.live = None
            await live.close()
    return state
