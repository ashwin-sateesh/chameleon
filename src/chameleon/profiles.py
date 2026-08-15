from __future__ import annotations

from typing import Literal

import yaml
from pydantic import BaseModel, Field

from chameleon.paths import configs_dir

RiskLevel = Literal["none", "ambiguous_choice", "needs_user_info", "irreversible"]


class LoginConfig(BaseModel):
    username: str
    password: str


class ChecklistItem(BaseModel):
    goal: str
    risk: RiskLevel = "none"


class SiteProfile(BaseModel):
    id: str
    name: str
    base_url: str
    requires_login: bool = False
    login: LoginConfig | None = None
    task_type: str
    checklist_template: list[ChecklistItem] = Field(default_factory=list)


class UnknownSiteError(ValueError):
    """Raised when --site does not match a YAML profile."""


def list_profile_ids(directory=None) -> list[str]:
    root = directory or configs_dir()
    if not root.is_dir():
        return []
    return sorted(path.stem for path in root.glob("*.yaml"))


def load_profile(site_id: str, directory=None) -> SiteProfile:
    root = directory or configs_dir()
    path = root / f"{site_id}.yaml"
    if not path.is_file():
        known = ", ".join(list_profile_ids(root)) or "(none)"
        raise UnknownSiteError(f"Unknown site {site_id!r}. Known profiles: {known}")
    raw = yaml.safe_load(path.read_text()) or {}
    raw["id"] = site_id
    return SiteProfile.model_validate(raw)
