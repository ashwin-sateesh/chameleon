"""Page fingerprints for copilot observe. Maps pans must not look like intent."""

from __future__ import annotations

import re
from typing import Literal
from urllib.parse import unquote, urlparse

BlockerKind = Literal["cookie", "captcha"]

_AT_RE = re.compile(r"/@[-\d.]+,[-\d.]+(?:,[-\d.]+[a-z]*)?", re.I)
_DATA_RE = re.compile(r"/data=[^/]*", re.I)
_TITLE_RE = re.compile(r"Page Title:\s*(.+)", re.I)
_HEADING_RE = re.compile(r'heading\s+"([^"]+)"', re.I)

_URL_PATTERNS = [
    re.compile(r"Page URL:\s*(\S+)"),
    re.compile(r"\(current\)[^\n]*\((https?://[^)]+)\)"),
    re.compile(r"^-+\s*url:\s*(\S+)", re.MULTILINE | re.IGNORECASE),
    re.compile(r"URL:\s*(https?://\S+)"),
]


def extract_url(snapshot: str) -> str | None:
    for pattern in _URL_PATTERNS:
        match = pattern.search(snapshot)
        if match:
            return match.group(1).rstrip(".,)")
    return None

_COOKIE_MARKERS = (
    "before you continue",
    "before you continue to google",
    "accept all",
    "reject all",
    "i agree to the use of cookies",
    "we use cookies",
    "cookie consent",
)
_CAPTCHA_MARKERS = (
    "unusual traffic",
    "i'm not a robot",
    "i am not a robot",
    "recaptcha",
    "/sorry/",
    "enable javascript",
    "detected unusual traffic from your computer",
)


def normalize_url(url: str) -> str:
    if not url:
        return ""
    parsed = urlparse(url.strip())
    path = unquote(parsed.path or "/")
    path = _AT_RE.sub("", path)
    path = _DATA_RE.sub("", path)
    path = re.sub(r"/{2,}", "/", path)
    path = path.rstrip("/") or "/"
    host = (parsed.netloc or "").lower()
    scheme = parsed.scheme or "https"
    query = parsed.query
    if "google." in host and "/maps" in path:
        query = ""
    if query:
        return f"{scheme}://{host}{path}?{query}".lower()
    return f"{scheme}://{host}{path}".lower()


def extract_title(snapshot: str) -> str:
    match = _TITLE_RE.search(snapshot or "")
    return (match.group(1).strip() if match else "")[:200]


def extract_heading(snapshot: str) -> str:
    match = _HEADING_RE.search(snapshot or "")
    return (match.group(1).strip() if match else "")[:200]


def extract_search_value(snapshot: str) -> str:
    for line in (snapshot or "").splitlines():
        lowered = line.lower()
        if not any(token in lowered for token in ("textbox", "searchbox", "combobox")):
            continue
        if not any(token in lowered for token in ("search", "query", "where to", "find")):
            continue
        after_ref = re.search(r"\[(?:ref=)?[^\]]+\]:\s*(.+)$", line)
        if after_ref:
            value = after_ref.group(1).strip().strip('"')
            if value and not value.startswith("["):
                return value[:200]
    return ""


def page_fingerprint(snapshot: str, url: str | None = None) -> str:
    resolved = url or extract_url(snapshot) or ""
    return "|".join(
        [
            normalize_url(resolved),
            extract_title(snapshot).lower(),
            extract_search_value(snapshot).lower(),
            extract_heading(snapshot).lower(),
        ]
    )


def detect_blocker(snapshot: str) -> BlockerKind | None:
    text = (snapshot or "").lower()
    if any(marker in text for marker in _CAPTCHA_MARKERS):
        return "captcha"
    if any(marker in text for marker in _COOKIE_MARKERS):
        return "cookie"
    return None


def blocker_question(kind: BlockerKind) -> str:
    if kind == "captcha":
        return (
            "I see a CAPTCHA or unusual-traffic check. "
            "Please solve it in the browser, then type ok."
        )
    return (
        "There's a cookie or consent dialog. "
        "Please accept or dismiss it in the browser, then type ok. "
        "I will not click it for you."
    )


def is_end_phrase(text: str, phrases: list[str]) -> bool:
    normalized = text.strip().lower().rstrip(".! ")
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized in {p.strip().lower() for p in phrases}
