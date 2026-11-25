"""Unit tests for the zero-DNS PAC script generator."""
import pytest

from relay.core.pac import generate_pac


def test_pac_has_no_is_in_net_and_routes_https_wss_http():
    pac = generate_pac(11893).decode("utf-8")
    assert "isInNet" not in pac
    assert 'proto !== "https:"' in pac
    assert 'proto !== "wss://"' in pac
    assert 'proto.substring(0, 5) !== "http:"' in pac
    assert 'PROXY 127.0.0.1:11893; DIRECT' in pac
    # Steam and Epic must NOT be bypassed by default (required on strict/restricted networks).
    assert "steampowered.com" not in pac
    assert "epicgames.com" not in pac


def test_pac_includes_custom_bypass_domains_safely():
    pac = generate_pac(11893, bypass_domains=["banka.com.tr", 'bad");x']).decode("utf-8")
    assert 'host === "banka.com.tr"' in pac
    assert 'dnsDomainIs(host, ".banka.com.tr")' in pac
    assert "bad" not in pac


def test_pac_rejects_invalid_port():
    with pytest.raises(ValueError):
        generate_pac(0)
    with pytest.raises(ValueError):
        generate_pac(70000)
