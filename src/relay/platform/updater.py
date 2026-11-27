"""Lightweight GitHub Releases update checker (non-blocking, fails silently offline)."""
from __future__ import annotations

from dataclasses import dataclass
import json
import re
from urllib.request import Request, urlopen

from relay import __version__

GITHUB_REPO = "Ferdi-krbk/Relay"
RELEASES_API_URL = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
RELEASES_PAGE_URL = f"https://github.com/{GITHUB_REPO}/releases/latest"

_SEMVER_RE = re.compile(r"^\s*v?(\d+)(?:\.(\d+))?(?:\.(\d+))?")


@dataclass(frozen=True)
class UpdateInfo:
    available: bool
    current_version: str
    latest_version: str
    release_url: str


def parse_version(tag: str) -> tuple[int, int, int] | None:
    """Parse a version tag like 'v2.1.0' or '2.1' into a comparable (major, minor, patch) tuple."""
    if not isinstance(tag, str):
        return None
    m = _SEMVER_RE.match(tag.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2) or 0), int(m.group(3) or 0))


def is_newer_version(latest: str, current: str) -> bool:
    latest_v = parse_version(latest)
    current_v = parse_version(current)
    if latest_v is None or current_v is None:
        return False
    return latest_v > current_v


def _sanitize_release_url(url: str) -> str:
    if isinstance(url, str) and url.startswith(f"https://github.com/{GITHUB_REPO}/"):
        return url
    return RELEASES_PAGE_URL


def check_for_updates(
    *,
    current_version: str = __version__,
    api_url: str = RELEASES_API_URL,
    timeout: float = 4.0,
) -> UpdateInfo | None:
    """Query GitHub Releases API for a newer Relay version; return None on any network/API failure."""
    req = Request(
        api_url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": f"Relay/{current_version}",
        },
    )
    try:
        with urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            payload = json.loads(resp.read().decode("utf-8", errors="replace"))
    except Exception:
        return None

    if not isinstance(payload, dict) or payload.get("draft") or payload.get("prerelease"):
        return None

    raw_tag = str(payload.get("tag_name") or "").strip()
    parsed = parse_version(raw_tag)
    if parsed is None:
        return None

    clean_latest = f"{parsed[0]}.{parsed[1]}.{parsed[2]}"
    available = is_newer_version(clean_latest, current_version)
    release_url = _sanitize_release_url(str(payload.get("html_url") or ""))
    return UpdateInfo(
        available=available,
        current_version=current_version,
        latest_version=clean_latest,
        release_url=release_url,
    )
