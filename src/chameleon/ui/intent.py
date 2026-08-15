"""Turn a chat message into a site profile when the UI has no site picker."""

from __future__ import annotations

import re
from urllib.parse import urlparse

from chameleon.profiles import SiteProfile, list_profile_ids, load_profile

_SHOPPING = ("buy", "cart", "checkout", "t-shirt", "tshirt", "shirt", "shop", "order")
_FORM_FILL = ("apply", "application", "job", "resume", "cover letter", "greenhouse")
_EXPLORE = {
    "maps": ("google maps", "maps", "restaurant", "restaurants", "directions"),
    "airbnb": ("airbnb", "stay", "stays", "airbnb.com"),
    "osm": ("openstreetmap", "osm"),
}
_TYPE_WORDS = {
    "shopping": _SHOPPING,
    "form_fill": _FORM_FILL,
    "exploration": ("maps", "restaurant", "airbnb", "stay", "directions", "openstreetmap"),
}


def load_profiles() -> list[SiteProfile]:
    return [load_profile(site_id) for site_id in list_profile_ids()]


def infer_site(text: str, profiles: list[SiteProfile] | None = None) -> str | None:
    """Return a profile id if the message uniquely points at one configured site."""
    needle = text.strip().lower()
    if not needle:
        return None
    known = profiles if profiles is not None else load_profiles()
    if not known:
        return None
    if len(known) == 1:
        return known[0].id

    scored: list[tuple[int, str]] = []
    for profile in known:
        score = _score(needle, profile)
        if score:
            scored.append((score, profile.id))
    scored.sort(reverse=True)
    if not scored:
        return None
    if len(scored) == 1 or scored[0][0] > scored[1][0]:
        return scored[0][1]
    return None


def _score(needle: str, profile: SiteProfile) -> int:
    score = 0
    if profile.id.lower() in needle or profile.name.lower() in needle:
        score += 8
    host = urlparse(profile.base_url).netloc.lower()
    if host and host in needle:
        score += 10
    if profile.base_url.rstrip("/").lower() in needle:
        score += 10
    for url in _urls(needle):
        parsed = urlparse(url)
        if host and parsed.netloc.lower() == host:
            score += 12
        if profile.base_url.rstrip("/").lower() in url.rstrip("/"):
            score += 12
    for word in _TYPE_WORDS.get(profile.task_type, ()):
        if word in needle:
            score += 2
    for word in _EXPLORE.get(profile.id, ()):
        if word in needle:
            score += 4
    return score


def _urls(text: str) -> list[str]:
    return re.findall(r"https?://[^\s]+", text, flags=re.IGNORECASE)


def is_stop(text: str) -> bool:
    return text.strip().lower() in {"stop", "cancel", "quit"}


def is_resume(text: str) -> bool:
    return text.strip().lower() in {"resume", "continue"}
