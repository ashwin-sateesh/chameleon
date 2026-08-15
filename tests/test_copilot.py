"""Terminal-led copilot without a live LLM or browser."""

from __future__ import annotations

import asyncio
from pathlib import Path

from chameleon.agents.guardian import GuardianVerdict, guardian_copilot_action
from chameleon.agents.navigator import NavigatorProposal
from chameleon.agents.planner import ObserveResult
from chameleon.copilot_loop import StopRequested, followup_question, run_copilot, task_is_specific
from chameleon.loop import MissingTaskError, run_task
from chameleon.profiles import load_profile
from chameleon.state import CopilotPhase, TaskStatus, load_state


class FakeMapsMCP:
    instances: list["FakeMapsMCP"] = []

    def __init__(self, *, user_data_dir: Path, output_dir: Path, **_: object) -> None:
        self.user_data_dir = user_data_dir
        self.output_dir = output_dir
        self.url = "about:blank"
        self.calls: list[tuple[str, dict]] = []
        self.restored = False
        self.snapshots = 0
        self.tab_places = ["home"]
        self.active_tab = 0
        FakeMapsMCP.instances.append(self)

    @property
    def place(self) -> str:
        return self.tab_places[self.active_tab]

    @place.setter
    def place(self, value: str) -> None:
        self.tab_places[self.active_tab] = value

    def open_tab(self, place: str) -> None:
        self.tab_places.append(place)

    async def __aenter__(self) -> FakeMapsMCP:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def _body_for(self, place: str) -> str:
        if place == "food":
            url = "https://www.google.com/maps/search/restaurants/@37.77,-122.41,12z"
            return (
                f"- Page URL: {url}\n- Page Title: Restaurants - Google Maps\n"
                '- textbox "Search Google Maps" [ref=e3]: restaurants in San Francisco\n'
                '- heading "Results" [ref=e20]\n'
                '- button "Guest favorite" [ref=e40]\n'
            )
        if place == "listing":
            url = "https://www.airbnb.com/rooms/123"
            return (
                f"- Page URL: {url}\n- Page Title: Cozy loft - Airbnb\n"
                '- heading "Cozy loft" [ref=e20]\n'
                '- button "Show reviews" [ref=e30]\n'
            )
        url = "https://www.google.com/maps"
        return (
            f"- Page URL: {url}\n- Page Title: Google Maps\n"
            '- textbox "Search Google Maps" [ref=e3]\n'
        )

    def _body(self) -> str:
        body = self._body_for(self.place)
        for line in body.splitlines():
            if line.startswith("- Page URL:"):
                self.url = line.split(":", 1)[1].strip()
                break
        return body

    def _tab_list(self) -> str:
        lines = ["### Open tabs"]
        for index, place in enumerate(self.tab_places):
            body = self._body_for(place)
            url = "about:blank"
            title = place
            for line in body.splitlines():
                if line.startswith("- Page URL:"):
                    url = line.split(":", 1)[1].strip()
                if line.startswith("- Page Title:"):
                    title = line.split(":", 1)[1].strip()
            current = " (current)" if index == self.active_tab else ""
            lines.append(f"- {index}:{current} [{title}] ({url})")
        return "\n".join(lines)

    async def snapshot(self) -> str:
        self.snapshots += 1
        return self._body()

    async def list_tabs(self) -> str:
        return await self.call_tool("browser_tabs", {"action": "list"})

    async def select_tab(self, index: int) -> str:
        return await self.call_tool("browser_tabs", {"action": "select", "index": index})

    async def navigate(self, url: str) -> str:
        return await self.call_tool("browser_navigate", {"url": url})

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if name == "browser_navigate":
            self.url = arguments["url"]
            if "maps" in self.url and "search" not in self.url:
                self.place = "home"
        if name == "browser_type":
            self.place = "food"
        if name == "browser_tabs":
            action = arguments.get("action")
            if action == "list":
                return self._tab_list()
            if action == "select":
                index = int(arguments.get("index") or 0)
                if 0 <= index < len(self.tab_places):
                    self.active_tab = index
                return await self.snapshot()
        return "ok"

    async def save_storage_state(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"cookies":[]}\n')
        return "saved"

    async def restore_storage_state(self, path: Path) -> str:
        self.restored = path.is_file()
        return "restored"


def _copy_sites(tmp_path: Path) -> None:
    sites = tmp_path / "configs" / "sites"
    sites.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='chameleon'\n")
    repo = Path(__file__).resolve().parents[1]
    for name in ("saucedemo.yaml", "greenhouse.yaml", "maps.yaml", "osm.yaml", "airbnb.yaml"):
        (sites / name).write_text((repo / "configs" / "sites" / name).read_text())


def _install_copilot(monkeypatch, tmp_path, *, observe=None, interpret=None, nav=None, reader=None):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    monkeypatch.setenv("CHAMELEON_COPILOT_POLL", "0.05")
    monkeypatch.setenv("CHAMELEON_COPILOT_DEBOUNCE", "1")
    _copy_sites(tmp_path)
    FakeMapsMCP.instances = []
    monkeypatch.setattr("chameleon.copilot_loop.PlaywrightMCP", FakeMapsMCP)

    nav_calls: list[str] = []

    def fake_observe(**kwargs):
        url = kwargs.get("url") or ""
        if "search/restaurants" in url:
            return ObserveResult(
                user_state="Restaurant results in San Francisco",
                possible_intents=["Hotels filter", "Things to do"],
                question="I'll apply Hotels. Hotels and Things to do are on this page. OK / which one / done?",
                should_ask=True,
                suggested_micro_goal="open the Hotels filter on this Maps results page",
            )
        return ObserveResult(user_state="Maps homepage", should_ask=False)

    def fake_interpret(**kwargs):
        reply = (kwargs.get("user_reply") or "").lower()
        goal = kwargs.get("suggested_micro_goal") or "search restaurants in San Francisco"
        if any(token in reply for token in ("yes", "ok", "do it", "go ahead")):
            return GuardianVerdict(decision="PROCEED", reason="confirmed", micro_goal=goal)
        if "food" in reply or "restaurant" in reply:
            return GuardianVerdict(
                decision="PROCEED",
                reason="instruction is consent",
                micro_goal="search restaurants in San Francisco and show the list",
            )
        return GuardianVerdict(decision="PROCEED", reason="instruction", micro_goal=goal)

    def fake_nav(**kwargs):
        nav_calls.append(kwargs["micro_goal"])
        return NavigatorProposal(
            tool="browser_type",
            arguments={"target": "e3", "text": "restaurants in San Francisco", "element": "Search"},
            reason="search food",
            subgoal_complete=True,
        )

    monkeypatch.setattr("chameleon.copilot_loop.planner_observe", observe or fake_observe)
    monkeypatch.setattr("chameleon.copilot_loop.guardian_copilot_interpret", interpret or fake_interpret)
    monkeypatch.setattr("chameleon.copilot_loop.navigator_copilot_step", nav or fake_nav)
    if reader is not None:
        monkeypatch.setattr("chameleon.copilot_loop.read_user_line", reader)
    return nav_calls


def test_task_is_specific():
    maps = load_profile("maps")
    assert not task_is_specific(maps, maps.default_task or "")
    assert not task_is_specific(maps, "accompany the user exploring Google Maps")
    assert task_is_specific(maps, "search restaurants in San Francisco")


def test_execute_site_requires_task(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    _copy_sites(tmp_path)
    try:
        asyncio.run(run_task("saucedemo", None, "need-task"))
    except MissingTaskError as exc:
        assert "saucedemo" in str(exc)
    else:
        raise AssertionError("expected MissingTaskError")


def test_copilot_observe_does_not_call_navigator(tmp_path, monkeypatch):
    async def reader() -> str:
        await asyncio.sleep(0.15)
        return "done"

    nav_calls = _install_copilot(monkeypatch, tmp_path, reader=reader)
    profile = load_profile("maps")
    state = asyncio.run(run_copilot(profile, profile.default_task or "explore", "obs1"))
    assert state.status == TaskStatus.completed
    assert nav_calls == []
    assert state.navigator_action_history == []
    assert state.phase == CopilotPhase.observing


def test_copilot_specific_task_confirm_then_act(tmp_path, monkeypatch):
    nav_calls = _install_copilot(monkeypatch, tmp_path)
    incoming: asyncio.Queue[str] = asyncio.Queue()

    async def driver() -> None:
        for _ in range(80):
            st = load_state("maps1")
            if st and st.pending_question and "OK?" in (st.pending_question or ""):
                await incoming.put("yes")
                break
            await asyncio.sleep(0.05)
        else:
            await incoming.put("done")
            return
        for _ in range(80):
            st = load_state("maps1")
            if st and st.navigator_action_history:
                await incoming.put("done")
                return
            await asyncio.sleep(0.05)
        await incoming.put("done")

    async def reader() -> str:
        return await incoming.get()

    monkeypatch.setattr("chameleon.copilot_loop.read_user_line", reader)

    async def run() -> None:
        task = asyncio.create_task(
            run_copilot(load_profile("maps"), "search restaurants in San Francisco", "maps1")
        )
        drive = asyncio.create_task(driver())
        await task
        drive.cancel()

    asyncio.run(run())
    state = load_state("maps1")
    assert state is not None
    assert state.status == TaskStatus.completed
    assert nav_calls
    assert any(a.answer == "yes" for a in state.guardian_answers)
    assert state.navigator_action_history
    assert FakeMapsMCP.instances[-1].place == "food"


def test_copilot_kill_during_ask_then_resume(tmp_path, monkeypatch):
    _install_copilot(monkeypatch, tmp_path)

    async def reader_stop() -> str:
        for _ in range(80):
            st = load_state("kill-maps")
            if st and st.pending_question:
                raise StopRequested
            await asyncio.sleep(0.05)
        return "done"

    monkeypatch.setattr("chameleon.copilot_loop.read_user_line", reader_stop)
    first = asyncio.run(
        run_copilot(load_profile("maps"), "search restaurants in San Francisco", "kill-maps")
    )
    saved = load_state("kill-maps")
    assert saved is not None
    assert first.status == TaskStatus.paused_ask
    assert saved.status == TaskStatus.paused_ask
    assert saved.pending_question
    assert saved.navigator_action_history == []

    async def reader_done() -> str:
        return "done"

    _install_copilot(monkeypatch, tmp_path, reader=reader_done)
    second = asyncio.run(
        run_copilot(load_profile("maps"), "search restaurants in San Francisco", "kill-maps")
    )
    assert second.status == TaskStatus.completed
    assert FakeMapsMCP.instances[-1].restored is True


def test_guardian_copilot_action_skips_llm_for_normal_click():
    ok = guardian_copilot_action(
        proposed_action={"tool": "browser_click", "arguments": {"target": "e3", "element": "Search"}},
        micro_goal="search restaurants",
        task="search restaurants",
    )
    assert ok.decision == "PROCEED"
    date_click = guardian_copilot_action(
        proposed_action={
            "tool": "browser_click",
            "arguments": {"target": "e9", "element": "23"},
            "reason": "Select Aug 23 as checkout date",
        },
        micro_goal="Search Airbnb stays near SFO for Aug 19-23, 5 guests",
        task="4+ star stays near SFO",
    )
    assert date_click.decision == "PROCEED"
    risky = guardian_copilot_action(
        proposed_action={"tool": "browser_click", "arguments": {"element": "Request to book"}},
        micro_goal="request to book this stay",
        task="find a stay",
    )
    assert risky.decision == "ASK"
    offsite = guardian_copilot_action(
        proposed_action={"tool": "browser_navigate", "arguments": {"url": "https://evil.example/pay"}},
        micro_goal="open listing",
        task="find a stay",
    )
    assert offsite.decision == "ASK"


def test_copilot_senses_user_page_click(tmp_path, monkeypatch):
    nav_calls = _install_copilot(monkeypatch, tmp_path)
    incoming: asyncio.Queue[str] = asyncio.Queue()
    saw_hotels = False

    async def driver() -> None:
        nonlocal saw_hotels
        for _ in range(80):
            if FakeMapsMCP.instances:
                break
            await asyncio.sleep(0.05)
        else:
            await incoming.put("done")
            return
        await asyncio.sleep(0.2)
        FakeMapsMCP.instances[-1].place = "food"
        for _ in range(80):
            st = load_state("click1")
            if st and st.pending_question and "Hotels" in (st.pending_question or ""):
                saw_hotels = True
                await incoming.put("done")
                return
            await asyncio.sleep(0.05)
        await incoming.put("done")

    async def reader() -> str:
        return await incoming.get()

    monkeypatch.setattr("chameleon.copilot_loop.read_user_line", reader)

    async def run() -> None:
        task = asyncio.create_task(
            run_copilot(load_profile("maps"), load_profile("maps").default_task or "explore", "click1")
        )
        drive = asyncio.create_task(driver())
        await task
        drive.cancel()

    asyncio.run(run())
    state = load_state("click1")
    assert state is not None
    assert state.status == TaskStatus.completed
    assert nav_calls == []
    assert saw_hotels


def test_copilot_action_limit_leaves_acting_and_suggests(tmp_path, monkeypatch):
    def never_done_nav(**kwargs):
        return NavigatorProposal(
            tool="browser_type",
            arguments={"target": "e3", "text": "restaurants", "element": "Search"},
            reason="keep typing",
            subgoal_complete=False,
        )

    _install_copilot(monkeypatch, tmp_path, nav=never_done_nav)
    monkeypatch.setattr("chameleon.copilot_loop.MAX_ACT_ACTIONS", 2)
    incoming: asyncio.Queue[str] = asyncio.Queue()

    async def driver() -> None:
        for _ in range(80):
            st = load_state("cap1")
            if st and st.pending_question and "OK?" in (st.pending_question or ""):
                await incoming.put("yes")
                break
            await asyncio.sleep(0.05)
        else:
            await incoming.put("done")
            return
        for _ in range(80):
            st = load_state("cap1")
            if st and st.phase == CopilotPhase.observing and st.pending_question:
                await incoming.put("done")
                return
            if st and st.phase == CopilotPhase.asking and st.pending_question and "OK?" not in (
                st.pending_question or ""
            ):
                await incoming.put("done")
                return
            await asyncio.sleep(0.05)
        await incoming.put("done")

    async def reader() -> str:
        return await incoming.get()

    monkeypatch.setattr("chameleon.copilot_loop.read_user_line", reader)

    async def run() -> None:
        task = asyncio.create_task(
            run_copilot(load_profile("maps"), "search restaurants in San Francisco", "cap1")
        )
        drive = asyncio.create_task(driver())
        await task
        drive.cancel()

    asyncio.run(run())
    state = load_state("cap1")
    assert state is not None
    assert state.status == TaskStatus.completed
    assert state.phase != CopilotPhase.acting
    assert len(state.navigator_action_history) == 2


def test_followup_question_missing_filter():
    missing = followup_question(unmet=["4+ stars"], extras=["Guest favorite", "Instant Book"])
    assert missing is not None
    assert "isn't on this page" in missing
    assert "Guest favorite" in missing
    assert followup_question(unmet=[], extras=[]) is None
    assert "Hotels" in (followup_question(unmet=[], extras=["Hotels filter"]) or "")


def test_copilot_missing_constraint_asks_then_skip_does_not_hunt(tmp_path, monkeypatch):
    def observe(**kwargs):
        url = kwargs.get("url") or ""
        if "search/restaurants" in url:
            return ObserveResult(
                user_state="Restaurant results",
                unmet_constraints=["4+ stars"],
                possible_intents=["Hotels filter"],
                should_ask=False,
            )
        return ObserveResult(user_state="Maps homepage", should_ask=False)

    def interpret(**kwargs):
        reply = (kwargs.get("user_reply") or "").lower()
        goal = kwargs.get("suggested_micro_goal") or "search restaurants in San Francisco"
        if any(token in reply for token in ("yes", "ok", "do it", "go ahead")):
            return GuardianVerdict(decision="PROCEED", reason="confirmed", micro_goal=goal)
        if "skip" in reply:
            return GuardianVerdict(decision="WAIT", reason="skip missing filter")
        return GuardianVerdict(decision="PROCEED", reason="instruction", micro_goal=goal)

    nav_calls = _install_copilot(monkeypatch, tmp_path, observe=observe, interpret=interpret)
    incoming: asyncio.Queue[str] = asyncio.Queue()
    saw_missing = False

    async def driver() -> None:
        nonlocal saw_missing
        for _ in range(80):
            st = load_state("miss1")
            if st and st.pending_question and "OK?" in (st.pending_question or ""):
                await incoming.put("yes")
                break
            await asyncio.sleep(0.05)
        else:
            await incoming.put("done")
            return
        for _ in range(80):
            st = load_state("miss1")
            if st and st.pending_question and "isn't on this page" in (st.pending_question or ""):
                saw_missing = True
                await incoming.put("skip it")
                break
            await asyncio.sleep(0.05)
        else:
            await incoming.put("done")
            return
        await asyncio.sleep(0.15)
        await incoming.put("done")

    async def reader() -> str:
        return await incoming.get()

    monkeypatch.setattr("chameleon.copilot_loop.read_user_line", reader)

    async def run() -> None:
        task = asyncio.create_task(
            run_copilot(load_profile("maps"), "4+ star restaurants in San Francisco", "miss1")
        )
        drive = asyncio.create_task(driver())
        await task
        drive.cancel()

    asyncio.run(run())
    state = load_state("miss1")
    assert state is not None
    assert saw_missing
    assert state.status == TaskStatus.completed
    assert len(nav_calls) == 1
    assert not any("star" in (call or "").lower() for call in nav_calls[1:])


def test_parse_browser_tabs_playwright_format():
    from chameleon.mcp_client import parse_browser_tabs

    raw = (
        "### Open tabs\n"
        "- 0: (current) [Airbnb] (https://www.airbnb.com/s/sfo)\n"
        "- 1: [Cozy loft] (https://www.airbnb.com/rooms/123)\n"
    )
    tabs = parse_browser_tabs(raw)
    assert [tab.index for tab in tabs] == [0, 1]
    assert tabs[0].current is True
    assert tabs[1].url == "https://www.airbnb.com/rooms/123"


def test_copilot_switches_to_user_opened_tab(tmp_path, monkeypatch):
    nav_calls = _install_copilot(monkeypatch, tmp_path)
    incoming: asyncio.Queue[str] = asyncio.Queue()
    saw_hotels = False

    async def driver() -> None:
        nonlocal saw_hotels
        for _ in range(80):
            if FakeMapsMCP.instances:
                break
            await asyncio.sleep(0.05)
        else:
            await incoming.put("done")
            return
        await asyncio.sleep(0.2)
        FakeMapsMCP.instances[-1].open_tab("food")
        for _ in range(80):
            st = load_state("tab1")
            if st and st.pending_question and "Hotels" in (st.pending_question or ""):
                saw_hotels = True
                await incoming.put("done")
                return
            await asyncio.sleep(0.05)
        await incoming.put("done")

    async def reader() -> str:
        return await incoming.get()

    monkeypatch.setattr("chameleon.copilot_loop.read_user_line", reader)

    async def run() -> None:
        task = asyncio.create_task(
            run_copilot(load_profile("maps"), load_profile("maps").default_task or "explore", "tab1")
        )
        drive = asyncio.create_task(driver())
        await task
        drive.cancel()

    asyncio.run(run())
    state = load_state("tab1")
    assert state is not None
    assert saw_hotels
    assert nav_calls == []
    assert FakeMapsMCP.instances[-1].active_tab == 1
