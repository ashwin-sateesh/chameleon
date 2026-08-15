from __future__ import annotations

from chameleon.fingerprint import (
    detect_blocker,
    extract_search_value,
    is_end_phrase,
    normalize_url,
    page_fingerprint,
)


def _snap(url: str, *, title: str = "Google Maps", heading: str = "", search: str = "") -> str:
    search_line = '- textbox "Search Google Maps" [ref=e3]'
    if search:
        search_line += f": {search}"
    heading_line = f'- heading "{heading}" [ref=e20]' if heading else ""
    return (
        f"- Page URL: {url}\n"
        f"- Page Title: {title}\n"
        f"{search_line}\n"
        f"{heading_line}\n"
    )


def test_maps_pan_does_not_change_fingerprint():
    snap = _snap(
        "https://www.google.com/maps/place/San+Francisco/@37.77,-122.41,12z",
        heading="San Francisco",
        search="San Francisco",
    )
    fp1 = page_fingerprint(
        snap,
        "https://www.google.com/maps/place/San+Francisco/@37.77,-122.41,12z",
    )
    fp2 = page_fingerprint(
        snap.replace("37.77,-122.41,12z", "37.80,-122.42,14z"),
        "https://www.google.com/maps/place/San+Francisco/@37.80,-122.42,14z",
    )
    assert fp1 == fp2


def test_place_vs_search_fingerprints_differ():
    place = page_fingerprint(
        _snap(
            "https://www.google.com/maps/place/San+Francisco/@37.77,-122.41,12z",
            heading="San Francisco",
            search="San Francisco",
        ),
        "https://www.google.com/maps/place/San+Francisco/@37.77,-122.41,12z",
    )
    food = page_fingerprint(
        _snap(
            "https://www.google.com/maps/search/restaurants/@37.77,-122.41,12z",
            heading="Results",
            search="restaurants",
        ),
        "https://www.google.com/maps/search/restaurants/@37.77,-122.41,12z",
    )
    assert place != food


def test_normalize_url_strips_maps_at_and_data():
    assert normalize_url(
        "https://www.google.com/maps/place/San+Francisco/@37.7,-122.4,12z/data=abc"
    ) == normalize_url("https://www.google.com/maps/place/San+Francisco")


def test_search_box_value_changes_fingerprint():
    url = "https://www.google.com/maps"
    empty = page_fingerprint(_snap(url), url)
    typed = page_fingerprint(_snap(url, search="San Francisco"), url)
    assert empty != typed
    assert extract_search_value(_snap(url, search="San Francisco")) == "San Francisco"


def test_detect_cookie_and_captcha():
    assert detect_blocker("Before you continue to Google\nAccept all") == "cookie"
    assert detect_blocker("Unusual traffic from your computer network") == "captcha"
    assert detect_blocker("San Francisco restaurants") is None


def test_dialog_changes_fingerprint():
    url = "https://www.airbnb.com/s/sfo"
    base = _snap(url, title="Airbnb", heading="Homes in San Francisco")
    dialog = base + '- dialog "Cozy loft" [ref=e99]\n'
    assert page_fingerprint(base, url) != page_fingerprint(dialog, url)
