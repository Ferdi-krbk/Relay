"""Tests for GitHub Releases update checker."""
from __future__ import annotations

import io
import json

from relay.platform.updater import (
    RELEASES_PAGE_URL,
    check_for_updates,
    is_newer_version,
    parse_version,
)


def test_version_parsing_and_comparison():
    assert parse_version("v2.1.0") == (2, 1, 0)
    assert parse_version("2.10.3") == (2, 10, 3)
    assert parse_version("2.2") == (2, 2, 0)
    assert parse_version("invalid") is None

    assert is_newer_version("v2.2.0", "2.1.0") is True
    assert is_newer_version("v2.10.0", "2.2.0") is True
    assert is_newer_version("v2.1.0", "2.1.0") is False
    assert is_newer_version("v2.0.9", "2.1.0") is False


def test_check_for_updates_detects_newer_release_and_sanitizes_url(monkeypatch):
    class _Resp:
        status = 200

        def __init__(self, body: dict):
            self._buf = io.BytesIO(json.dumps(body).encode("utf-8"))

        def read(self):
            return self._buf.read()

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(
        "relay.platform.updater.urlopen",
        lambda req, timeout=4.0: _Resp(
            {
                "tag_name": "v2.2.0",
                "html_url": "https://github.com/Ferdi-krbk/Relay/releases/tag/v2.2.0",
                "draft": False,
                "prerelease": False,
            }
        ),
    )
    info = check_for_updates(current_version="2.1.0")
    assert info is not None
    assert info.available is True
    assert info.latest_version == "2.2.0"
    assert info.release_url == "https://github.com/Ferdi-krbk/Relay/releases/tag/v2.2.0"

    # Untrusted URL must fall back to official GitHub releases page
    monkeypatch.setattr(
        "relay.platform.updater.urlopen",
        lambda req, timeout=4.0: _Resp(
            {
                "tag_name": "v2.3.0",
                "html_url": "https://evil.example/phish",
                "draft": False,
                "prerelease": False,
            }
        ),
    )
    info2 = check_for_updates(current_version="2.1.0")
    assert info2 is not None and info2.available is True
    assert info2.release_url == RELEASES_PAGE_URL


def test_check_for_updates_fails_silently_offline(monkeypatch):
    def _boom(*_a, **_kw):
        raise OSError("offline")

    monkeypatch.setattr("relay.platform.updater.urlopen", _boom)
    assert check_for_updates(current_version="2.1.0") is None
