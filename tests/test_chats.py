from chameleon.ui.chats import (
    append_message,
    chat_summaries,
    create_chat,
    empty_draft,
    ensure_chat,
    load_chat,
    new_task_id,
    valid_task_id,
)


def test_create_and_list_chats(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")

    first = create_chat()
    assert valid_task_id(first.task_id)
    assert first.title == "New chat"
    assert load_chat(first.task_id) is not None

    append_message(first.task_id, "user", "buy me a t-shirt")
    loaded = load_chat(first.task_id)
    assert loaded is not None
    assert loaded.title == "buy me a t-shirt"
    assert loaded.messages[0].message == "buy me a t-shirt"
    assert loaded.messages[0].at

    summaries = chat_summaries()
    assert summaries[0]["task_id"] == first.task_id
    assert summaries[0]["status"] == "new"


def test_append_dedupes_identical_tail(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    chat = create_chat()
    append_message(chat.task_id, "ask", "Which shirt?")
    append_message(chat.task_id, "ask", "Which shirt?")
    loaded = load_chat(chat.task_id)
    assert loaded is not None
    assert len(loaded.messages) == 1


def test_empty_draft_reused(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    created = create_chat()
    draft = empty_draft()
    assert draft is not None
    assert draft.task_id == created.task_id


def test_rejects_unsafe_ids(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    assert not valid_task_id("../etc/passwd")
    assert load_chat("../x") is None
    assert ensure_chat("ok-id-1").task_id == "ok-id-1"
    assert new_task_id().startswith("chat-")


def test_title_uses_task_not_placeholder(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    from chameleon.profiles import ChecklistItem
    from chameleon.state import new_state, save_state

    save_state(
        new_state(
            site="saucedemo",
            task="buy me a t-shirt",
            task_id="demo1",
            checklist=[ChecklistItem(goal="log in", risk="none")],
        )
    )
    ensure_chat("demo1")
    loaded = load_chat("demo1")
    assert loaded is not None
    assert loaded.title == "buy me a t-shirt"
    summaries = chat_summaries()
    assert summaries[0]["title"] == "buy me a t-shirt"


def test_task_files_are_not_listed_as_chats(tmp_path, monkeypatch):
    monkeypatch.setenv("CHAMELEON_ROOT", str(tmp_path))
    (tmp_path / "configs" / "sites").mkdir(parents=True)
    (tmp_path / "pyproject.toml").write_text("[project]\nname='t'\n")
    from chameleon.profiles import ChecklistItem
    from chameleon.state import new_state, save_state

    save_state(
        new_state(
            site="saucedemo",
            task="buy me a t-shirt",
            task_id="demo1",
            checklist=[ChecklistItem(goal="log in", risk="none")],
        )
    )
    assert chat_summaries() == []
