from chameleon.profiles import SiteProfile
from chameleon.ui.intent import infer_site, is_resume, is_stop


def _profiles() -> list[SiteProfile]:
    return [
        SiteProfile(
            id="saucedemo",
            name="Sauce Demo",
            base_url="https://www.saucedemo.com/",
            task_type="shopping",
        ),
        SiteProfile(
            id="greenhouse",
            name="Greenhouse Job Application",
            base_url="https://job-boards.greenhouse.io/gitlab/jobs/8503792002",
            task_type="form_fill",
        ),
        SiteProfile(
            id="maps",
            name="Google Maps",
            base_url="https://www.google.com/maps",
            task_type="exploration",
            interaction_mode="copilot",
        ),
        SiteProfile(
            id="airbnb",
            name="Airbnb",
            base_url="https://www.airbnb.com",
            task_type="exploration",
            interaction_mode="copilot",
        ),
        SiteProfile(
            id="osm",
            name="OpenStreetMap",
            base_url="https://www.openstreetmap.org",
            task_type="exploration",
            interaction_mode="copilot",
        ),
    ]


def test_infer_shopping_task():
    assert infer_site("buy me a t-shirt", _profiles()) == "saucedemo"


def test_infer_job_application():
    assert infer_site("apply to this job for me", _profiles()) == "greenhouse"


def test_infer_maps_and_airbnb():
    profiles = _profiles()
    assert infer_site("restaurants in San Francisco", profiles) == "maps"
    assert infer_site("google maps", profiles) == "maps"
    assert infer_site("4+ star stays near SFO", profiles) == "airbnb"
    assert infer_site("openstreetmap downtown austin", profiles) == "osm"


def test_infer_by_url_and_name():
    profiles = _profiles()
    assert infer_site("open https://www.saucedemo.com/ and log in", profiles) == "saucedemo"
    assert infer_site("Sauce Demo", profiles) == "saucedemo"
    assert infer_site("greenhouse", profiles) == "greenhouse"


def test_infer_asks_when_ambiguous():
    assert infer_site("help me with this", _profiles()) is None


def test_stop_and_resume_phrases():
    assert is_stop("stop")
    assert is_stop("Cancel")
    assert not is_stop("stop the order from completing")
    assert is_resume("continue")
    assert not is_resume("continue filling the form")
