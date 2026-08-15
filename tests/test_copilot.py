"""Copilot observe/ask/act without a live LLM or browser."""

from __future__ import annotations

import asyncio
from pathlib import Path

from chameleon.agents.guardian import GuardianVerdict
from chameleon.agents.navigator import NavigatorProposal
from chameleon.agents.planner import ObserveResult
from chameleon.copilot_loop import StopRequested, run_copilot
from chameleon.loop import MissingTaskError, run_task
from chameleon.profiles import load_profile
from chameleon.state import CopilotPhase, TaskStatus, load_state


class FakeMapsMCP:
    instances: list["FakeMapsMCP"] = []

    def __init__(self, *, user_data_dir: Path, output_dir: Path) -> None:
        self.user_data_dir = user_data_dir
        self.output_dir = output_dir
        self.url = "about:blank"
        self.place = "home"
        self.calls: list[tuple[str, dict]] = []
        self.restored = False
        self.snapshots = 0
        self.auto_sf_after = 0
        FakeMapsMCP.instances.append(self)

    async def __aenter__(self) -> FakeMapsMCP:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    def _body(self) -> str:
        if self.place == "sf":
            self.url = "https://www.google.com/maps/place/San+Francisco/@37.77,-122.41,12z"
            return (
                f"- Page URL: {self.url}\n- Page Title: San Francisco - Google Maps\n"
                '- textbox "Search Google Maps" [ref=e3]: San Francisco\n'
                '- heading "San Francisco" [ref=e20]\n'
            )
        if self.place == "food":
            self.url = "https://www.google.com/maps/search/restaurants/@37.77,-122.41,12z"
            return (
                f"- Page URL: {self.url}\n- Page Title: Restaurants - Google Maps\n"
                '- textbox "Search Google Maps" [ref=e3]: restaurants in San Francisco\n'
                '- heading "Results" [ref=e20]\n'
            )
        self.url = "https://www.google.com/maps"
        return (
            f"- Page URL: {self.url}\n- Page Title: Google Maps\n"
            '- textbox "Search Google Maps" [ref=e3]\n'
        )

    async def snapshot(self) -> str:
        self.snapshots += 1
        if self.auto_sf_after and self.snapshots >= self.auto_sf_after and self.place == "home":
            self.place = "sf"
        return self._body()

    async def navigate(self, url: str) -> str:
        return await self.call_tool("browser_navigate", {"url": url})

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if name == "browser_navigate":
            self.url = arguments["url"]
            if "maps" in self.url and "place" not in self.url and "search" not in self.url:
                self.place = "home"
        if name == "browser_type":
            self.place = "food"
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


def _install_copilot(monkeypatch, tmp_path, *, observe=None, interpret=None, action=None, nav=None, reader=None):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    monkeypatch.setenv("CHAMELEON_COPILOT_POLL", "0.05")
    monkeypatch.setenv("CHAMELEON_COPILOT_DEBOUNCE", "2")
    _copy_sites(tmp_path)
    FakeMapsMCP.instances = []
    monkeypatch.setattr("chameleon.copilot_loop.PlaywrightMCP", FakeMapsMCP)

    nav_calls: list[str] = []

    def fake_observe(**kwargs):
        url = kwargs.get("url") or ""
        if "place/San" in url or "San+Francisco" in url:
            return ObserveResult(
                user_state="Viewing San Francisco",
                possible_intents=["tourist places", "food", "lodging", "directions"],
                question="Want tourist spots, food, lodging, or keep exploring?",
                should_ask=True,
            )
        return ObserveResult(user_state="Maps homepage", should_ask=False)

    def fake_interpret(**kwargs):
        reply = (kwargs.get("user_reply") or "").lower()
        if "food" in reply:
            return GuardianVerdict(
                decision="ASK",
                question="I'll search restaurants in San Francisco. Should I do that, or will you?",
                reason="intent is not consent",
                micro_goal="search restaurants in San Francisco and show the list",
            )
        if "do it" in reply or "go ahead" in reply:
            return GuardianVerdict(
                decision="PROCEED",
                reason="user consented",
                micro_goal=kwargs.get("suggested_micro_goal")
                or "search restaurants in San Francisco and show the list",
            )
        if "i'll do it" in reply or "ill do it" in reply:
            return GuardianVerdict(decision="WAIT", reason="user will click")
        return GuardianVerdict(decision="WAIT", reason="unclear")

    def fake_action(**kwargs):
        return GuardianVerdict(decision="PROCEED", reason="consented burst")

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
    monkeypatch.setattr("chameleon.copilot_loop.guardian_copilot_action", action or fake_action)
    monkeypatch.setattr("chameleon.copilot_loop.navigator_copilot_step", nav or fake_nav)
    if reader is not None:
        monkeypatch.setattr("chameleon.copilot_loop.read_user_line", reader)
    return nav_calls


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
        await asyncio.sleep(0.2)
        return "done"

    nav_calls = _install_copilot(monkeypatch, tmp_path, reader=reader)
    profile = load_profile("maps")
    state = asyncio.run(run_copilot(profile, profile.default_task or "explore", "obs1"))
    assert state.status == TaskStatus.completed
    assert nav_calls == []
    assert state.navigator_action_history == []
    assert state.phase == CopilotPhase.observing


def test_copilot_sf_food_consent_then_act(tmp_path, monkeypatch):
    nav_calls = _install_copilot(monkeypatch, tmp_path)

    async def driver() -> None:
        for _ in range(80):
            st = load_state("maps1")
            if st and st.pending_question and "food" in (st.pending_question or "").lower():
                await incoming.put("food")
                break
            await asyncio.sleep(0.05)
        else:
            await incoming.put("done")
            return
        for _ in range(80):
            st = load_state("maps1")
            q = (st.pending_question or "") if st else ""
            if "Should I do that" in q or "restaurants" in q.lower():
                await incoming.put("do it")
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

    incoming: asyncio.Queue[str] = asyncio.Queue()

    async def reader() -> str:
        return await incoming.get()

    monkeypatch.setattr("chameleon.copilot_loop.read_user_line", reader)

    async def run() -> None:
        FakeMapsMCP.instances = []
        # Recreate after install so auto_sf applies to the instance run_copilot constructs.
        task = asyncio.create_task(run_copilot(load_profile("maps"), "explore maps", "maps1"))
        await asyncio.sleep(0.05)
        if FakeMapsMCP.instances:
            FakeMapsMCP.instances[-1].auto_sf_after = 2
        drive = asyncio.create_task(driver())
        await task
        drive.cancel()

    asyncio.run(run())
    state = load_state("maps1")
    assert state is not None
    assert state.status == TaskStatus.completed
    assert nav_calls
    assert any(a.answer == "food" for a in state.guardian_answers)
    assert any(a.answer == "do it" for a in state.guardian_answers)
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

    async def first() -> None:
        task = asyncio.create_task(run_copilot(load_profile("maps"), "explore maps", "kill-maps"))
        await asyncio.sleep(0.05)
        if FakeMapsMCP.instances:
            FakeMapsMCP.instances[-1].auto_sf_after = 2
        await task

    asyncio.run(first())
    saved = load_state("kill-maps")
    assert saved is not None
    assert saved.status == TaskStatus.paused_ask
    assert saved.pending_question
    assert saved.navigator_action_history == []

    async def reader_done() -> str:
        return "done"

    _install_copilot(monkeypatch, tmp_path, reader=reader_done)
    second = asyncio.run(run_copilot(load_profile("maps"), "explore maps", "kill-maps"))
    assert second.status == TaskStatus.completed
    assert FakeMapsMCP.instances[-1].restored is True
    assert any("food" in (a.question or "").lower() for a in second.guardian_answers) or second.pending_question is None
