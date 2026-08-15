from __future__ import annotations

from chameleon.paths import storage_state_path, task_state_path
from chameleon.profiles import ChecklistItem
from chameleon.state import TaskState, TaskStatus, load_state, new_state, save_state


def test_save_load_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")

    state = new_state(
        site="saucedemo",
        task="buy me a t-shirt",
        task_id="demo1",
        checklist=[
            ChecklistItem(goal="log in", risk="none"),
            ChecklistItem(goal="pick item", risk="ambiguous_choice"),
        ],
        current_url="https://www.saucedemo.com/",
    )
    state.navigator_action_history = []
    save_state(state)

    path = task_state_path("demo1")
    assert path.is_file()
    loaded = load_state("demo1")
    assert loaded is not None
    assert loaded.task == "buy me a t-shirt"
    assert loaded.site == "saucedemo"
    assert loaded.current_subgoal_index == 0
    assert loaded.status == TaskStatus.running
    assert len(loaded.planner_checklist) == 2
    assert loaded.planner_checklist[1].risk == "ambiguous_choice"
    assert loaded.storage_state_path == str(storage_state_path("demo1"))


def test_copilot_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")

    from chameleon.state import CopilotPhase, ObservedEvent

    state = new_state(
        site="maps",
        task="explore",
        task_id="maps1",
        checklist=[],
        current_url="https://www.google.com/maps",
        phase=CopilotPhase.observing,
    )
    state.user_state = "Maps homepage"
    state.last_fingerprint = "fp1"
    state.interpreted_fingerprint = "fp1"
    state.observed_events = [ObservedEvent(url=state.current_url, summary="home")]
    save_state(state)
    loaded = load_state("maps1")
    assert loaded is not None
    assert loaded.phase == CopilotPhase.observing
    assert loaded.user_state == "Maps homepage"
    assert loaded.interpreted_fingerprint == "fp1"
    assert loaded.observed_events[0].summary == "home"


def test_atomic_replace(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")

    state = TaskState(site="s", task="t", task_id="x", status=TaskStatus.running)
    save_state(state)
    state.status = TaskStatus.paused_ask
    state.pending_question = "Which shirt?"
    save_state(state)
    loaded = load_state("x")
    assert loaded is not None
    assert loaded.status == TaskStatus.paused_ask
    assert loaded.pending_question == "Which shirt?"
    assert not task_state_path("x").with_suffix(".json.tmp").exists()


def test_page_state_round_trip(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    state = TaskState(
        site="greenhouse",
        task="apply",
        task_id="form1",
        page_state={
            "url": "https://example.com/apply",
            "fields": [{"id": "first_name", "name": "first_name", "value": "Jane", "type": "text"}],
        },
    )
    save_state(state)
    loaded = load_state("form1")
    assert loaded is not None
    assert loaded.page_state is not None
    assert loaded.page_state["fields"][0]["value"] == "Jane"


def test_missing_state_returns_none(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    assert load_state("nope") is None
