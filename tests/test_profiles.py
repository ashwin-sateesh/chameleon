from __future__ import annotations

import pytest

from chameleon.profiles import UnknownSiteError, list_profile_ids, load_profile


def test_load_saucedemo():
    profile = load_profile("saucedemo")
    assert profile.id == "saucedemo"
    assert profile.requires_login is True
    assert profile.login is not None
    assert profile.login.username == "standard_user"
    assert profile.checklist_template[0].goal
    assert {item.risk for item in profile.checklist_template} <= {
        "none",
        "ambiguous_choice",
        "needs_user_info",
        "irreversible",
    }


def test_load_greenhouse():
    profile = load_profile("greenhouse")
    assert profile.requires_login is False
    assert profile.task_type == "form_fill"
    assert profile.base_url.startswith("http")
    assert any(item.risk == "irreversible" for item in profile.checklist_template)


def test_unknown_site():
    with pytest.raises(UnknownSiteError) as exc:
        load_profile("not-a-real-site")
    assert "not-a-real-site" in str(exc.value)


def test_list_profiles_includes_both():
    ids = list_profile_ids()
    assert "saucedemo" in ids
    assert "greenhouse" in ids
    assert "maps" in ids
    assert "osm" in ids
    assert "airbnb" in ids


def test_execute_profiles_default_interaction_mode():
    assert load_profile("saucedemo").interaction_mode == "execute"
    assert load_profile("greenhouse").interaction_mode == "execute"


def test_load_copilot_profiles():
    maps = load_profile("maps")
    assert maps.interaction_mode == "copilot"
    assert maps.intent_hints
    assert maps.default_task
    assert "done" in maps.end_phrases
    osm = load_profile("osm")
    assert osm.interaction_mode == "copilot"
    assert osm.base_url.startswith("http")
    airbnb = load_profile("airbnb")
    assert airbnb.interaction_mode == "copilot"
    assert "book" in " ".join(airbnb.intent_hints).lower() or "listing" in " ".join(airbnb.intent_hints).lower()
