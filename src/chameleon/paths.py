"""Repo root, configs, and runtime data paths."""

from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    env = os.environ.get("CHAMELEON_ROOT")
    if env:
        return Path(env).resolve()
    cwd = Path.cwd()
    for candidate in [cwd, *cwd.parents]:
        if (candidate / "configs" / "sites").is_dir() and (candidate / "pyproject.toml").is_file():
            return candidate
    return Path(__file__).resolve().parents[2]


def configs_dir() -> Path:
    return repo_root() / "configs" / "sites"


def data_dir() -> Path:
    return repo_root() / "data"


def task_state_path(task_id: str) -> Path:
    return data_dir() / "tasks" / f"{task_id}.json"


def session_dir(task_id: str) -> Path:
    return data_dir() / "sessions" / task_id


def storage_state_path(task_id: str) -> Path:
    return session_dir(task_id) / "storage_state.json"
