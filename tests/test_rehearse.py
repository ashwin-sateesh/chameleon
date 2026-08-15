"""Architectural rehearsal: Sauce Demo kill/resume and Greenhouse, without a live Grok key."""

from __future__ import annotations

import asyncio
from pathlib import Path

from chameleon.agents.guardian import GuardianVerdict
from chameleon.agents.navigator import NavigatorProposal
from chameleon.loop import StopRequested, run_task
from chameleon.profiles import load_profile
from chameleon.state import TaskStatus, load_state


class FakeMCP:
    instances: list["FakeMCP"] = []

    def __init__(self, *, user_data_dir: Path, output_dir: Path, headless: bool = False, cdp_endpoint: str | None = None, **_: object) -> None:
        self.user_data_dir = user_data_dir
        self.output_dir = output_dir
        self.headless = headless
        self.url = "about:blank"
        self.calls: list[tuple[str, dict]] = []
        self.restored = False
        FakeMCP.instances.append(self)

    async def __aenter__(self) -> FakeMCP:
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def navigate(self, url: str) -> str:
        return await self.call_tool("browser_navigate", {"url": url})

    async def snapshot(self) -> str:
        return (
            f"### Page\n- Page URL: {self.url}\n- Page Title: fake\n"
            "### Snapshot\n- button Login [ref=e15]"
        )

    async def call_tool(self, name: str, arguments: dict | None = None) -> str:
        arguments = arguments or {}
        self.calls.append((name, arguments))
        if name == "browser_navigate":
            self.url = arguments["url"]
        if name == "browser_click" and arguments.get("element") == "Login":
            self.url = "https://www.saucedemo.com/inventory.html"
        if name == "browser_click" and "Finish" in str(arguments.get("element", "")):
            self.url = "https://www.saucedemo.com/checkout-complete.html"
        return "ok"

    async def save_storage_state(self, path: Path) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text('{"cookies":[]}\n')
        return "saved"

    async def restore_storage_state(self, path: Path) -> str:
        self.restored = path.is_file()
        return "restored"


def _install_fakes(monkeypatch, tmp_path, *, proposals, answers, planner_calls):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    sites = tmp_path / "configs" / "sites"
    sites.mkdir(parents=True, exist_ok=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='chameleon'\n")
    repo = Path(__file__).resolve().parents[1]
    for name in ("saucedemo.yaml", "greenhouse.yaml"):
        (sites / name).write_text((repo / "configs" / "sites" / name).read_text())

    FakeMCP.instances = []
    monkeypatch.setattr("chameleon.loop.PlaywrightMCP", FakeMCP)

    nav_state = {
        "i": 0,
        "last_history": 0,
        "last_index": 0,
        "last_answers": 0,
        "last_was_empty_complete": False,
        "last_was_blocked": False,
        "started": False,
    }

    def fake_navigator(**kwargs):
        hist = len(kwargs["history"])
        idx = kwargs["subgoal_index"]
        answers = len(kwargs["guardian_answers"])
        if nav_state["started"]:
            if hist > nav_state["last_history"]:
                nav_state["i"] += 1
            elif idx > nav_state["last_index"] and nav_state["last_was_empty_complete"]:
                nav_state["i"] += 1
            elif answers > nav_state["last_answers"] and nav_state["last_was_blocked"]:
                nav_state["i"] += 1
        nav_state["started"] = True
        nav_state["last_history"] = hist
        nav_state["last_index"] = idx
        nav_state["last_answers"] = answers
        if nav_state["i"] >= len(proposals):
            return NavigatorProposal(tool=None, reason="stall", subgoal_complete=True)
        proposal = proposals[nav_state["i"]]
        nav_state["last_was_empty_complete"] = proposal.tool is None and proposal.subgoal_complete
        nav_state["last_was_blocked"] = proposal.tool is None and not proposal.subgoal_complete
        return proposal

    monkeypatch.setattr("chameleon.loop.navigator_step", fake_navigator)

    def fake_guardian(**kwargs):
        subgoal = kwargs["subgoal"]
        action = kwargs["proposed_action"]
        prior = kwargs["guardian_answers"]
        if subgoal.risk == "none":
            raise AssertionError("Guardian invoked on risk=none")
        if action.get("tool") in {"browser_snapshot", "browser_navigate"}:
            return GuardianVerdict(decision="PROCEED", reason="navigation")
        if any(subgoal.goal in a.question for a in prior):
            return GuardianVerdict(decision="PROCEED", reason="already answered")
        return GuardianVerdict(
            decision="ASK",
            question=f"Confirm: {subgoal.goal}?",
            reason=subgoal.risk,
        )

    monkeypatch.setattr("chameleon.loop.guardian_check", fake_guardian)

    real_planner = __import__("chameleon.agents.planner", fromlist=["planner_plan"]).planner_plan

    def counting_planner(task, profile):
        planner_calls.append(profile.id)
        return real_planner(task, profile)

    monkeypatch.setattr("chameleon.loop.planner_plan", counting_planner)

    ans_i = {"n": 0}

    async def fake_ask(question: str, options: list | None = None) -> str:
        del options
        if ans_i["n"] >= len(answers):
            raise StopRequested
        reply = answers[ans_i["n"]]
        ans_i["n"] += 1
        if reply == "__STOP__":
            raise StopRequested
        return reply

    monkeypatch.setattr("chameleon.loop._ask_user", fake_ask)


SAUCE_PROPOSALS = [
    NavigatorProposal(
        tool="browser_type",
        arguments={"element": "Username", "target": "e11", "text": "standard_user"},
        reason="username",
    ),
    NavigatorProposal(
        tool="browser_type",
        arguments={"element": "Password", "target": "e13", "text": "secret_sauce"},
        reason="password",
    ),
    NavigatorProposal(
        tool="browser_click",
        arguments={"element": "Login", "target": "e15"},
        reason="login",
        subgoal_complete=True,
    ),
    NavigatorProposal(
        tool="browser_click",
        arguments={"element": "Sauce Labs Bolt T-Shirt", "target": "e70"},
        reason="pick bolt t-shirt",
        subgoal_complete=True,
    ),
    NavigatorProposal(
        tool="browser_click",
        arguments={"element": "Add to cart", "target": "e54"},
        reason="add",
        subgoal_complete=True,
    ),
    NavigatorProposal(
        tool="browser_click",
        arguments={"element": "Checkout", "target": "e80"},
        reason="checkout",
    ),
    NavigatorProposal(
        tool="browser_type",
        arguments={"element": "Postal Code", "target": "e90", "text": "94107"},
        reason="zip",
        subgoal_complete=True,
    ),
    NavigatorProposal(
        tool="browser_click",
        arguments={"element": "Finish", "target": "e99"},
        reason="place order",
        subgoal_complete=True,
    ),
]


def test_saucedemo_e2e_and_greenhouse(tmp_path, monkeypatch):
    planner_calls: list[str] = []
    _install_fakes(
        monkeypatch,
        tmp_path,
        proposals=SAUCE_PROPOSALS,
        answers=["Bolt T-Shirt", "Jane Tester 94107", "yes finish"],
        planner_calls=planner_calls,
    )

    state = asyncio.run(run_task("saucedemo", "buy me a t-shirt", "demo1"))
    assert state.status == TaskStatus.completed
    assert planner_calls == ["saucedemo"]
    assert any(a.answer == "Bolt T-Shirt" for a in state.guardian_answers)
    assert load_state("demo1") is not None
    dump = tmp_path / "data" / "sessions" / "demo1" / "storage_state.json"
    assert dump.is_file()
    assert "inventory" in (state.current_url or "") or state.current_url

    planner_calls.clear()
    gh_proposals = [
        NavigatorProposal(
            tool="browser_navigate",
            arguments={"url": load_profile("greenhouse").base_url},
            reason="open",
            subgoal_complete=True,
        ),
        NavigatorProposal(
            tool="browser_type",
            arguments={"element": "Email", "target": "e2", "text": "jane@example.com"},
            reason="email",
            subgoal_complete=True,
        ),
        NavigatorProposal(
            tool="browser_type",
            arguments={"element": "Yes", "target": "e3", "text": "yes"},
            reason="screening",
            subgoal_complete=True,
        ),
        NavigatorProposal(tool=None, reason="no resume required", subgoal_complete=True),
        NavigatorProposal(
            tool="browser_click",
            arguments={"element": "Submit", "target": "e9"},
            reason="submit",
            subgoal_complete=True,
        ),
    ]
    _install_fakes(
        monkeypatch,
        tmp_path,
        proposals=gh_proposals,
        answers=["Jane Tester", "yes I am authorized", "skip resume", "do not submit yet"],
        planner_calls=planner_calls,
    )
    gh = asyncio.run(run_task("greenhouse", "apply to this job for me", "demo-gh"))
    assert gh.status == TaskStatus.completed
    assert planner_calls == ["greenhouse"]
    assert gh.site == "greenhouse"


def test_kill_during_ask_then_resume_without_replanning(tmp_path, monkeypatch):
    planner_calls: list[str] = []
    _install_fakes(
        monkeypatch,
        tmp_path,
        proposals=SAUCE_PROPOSALS,
        answers=["__STOP__"],
        planner_calls=planner_calls,
    )
    first = asyncio.run(run_task("saucedemo", "buy me a t-shirt", "demo-kill"))
    assert first.status == TaskStatus.paused_ask
    assert first.pending_question
    assert planner_calls == ["saucedemo"]
    assert first.current_subgoal_index == 1

    planner_calls.clear()
    _install_fakes(
        monkeypatch,
        tmp_path,
        proposals=SAUCE_PROPOSALS[3:],
        answers=["Bolt T-Shirt", "Jane Tester 94107", "yes"],
        planner_calls=planner_calls,
    )
    second = asyncio.run(run_task("saucedemo", "buy me a t-shirt", "demo-kill"))
    assert planner_calls == []
    assert second.status == TaskStatus.completed
    assert FakeMCP.instances[-1].restored is True
    assert any(a.answer == "Bolt T-Shirt" for a in second.guardian_answers)


def test_blocked_navigator_asks_instead_of_snapshot_loop(tmp_path, monkeypatch):
    planner_calls: list[str] = []
    _install_fakes(
        monkeypatch,
        tmp_path,
        proposals=[
            NavigatorProposal(
                tool="browser_navigate",
                arguments={"url": load_profile("greenhouse").base_url},
                reason="open",
                subgoal_complete=True,
            ),
            NavigatorProposal(tool=None, reason="need name email phone", subgoal_complete=False),
            NavigatorProposal(
                tool="browser_type",
                arguments={"element": "First name", "target": "e1", "text": "Jane"},
                reason="fill",
                subgoal_complete=True,
            ),
        ],
        answers=["Jane Tester, jane@example.com, 555-0100", "__STOP__"],
        planner_calls=planner_calls,
    )
    state = asyncio.run(run_task("greenhouse", "apply to this job for me", "demo-ask-loop"))
    assert any(a.answer.startswith("Jane Tester") for a in state.guardian_answers)
    assert any(name == "browser_type" for name, _ in FakeMCP.instances[-1].calls)
