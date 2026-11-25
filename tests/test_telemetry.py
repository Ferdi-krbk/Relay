"""Unit tests for ProxyTelemetry counters, ring buffer, and snapshot."""
from relay.core.telemetry import ProxyTelemetry


def test_telemetry_tracks_connections_bytes_and_events():
    tel = ProxyTelemetry(mode="auto", doh_enabled=True, max_events=3)
    tel.on_connection_open()
    tel.add_bytes(1000, is_upload=True)
    tel.add_bytes(5000, is_upload=False)
    tel.record_event("discord.com", "tcp_frag", True, 180, "DoH")
    tel.record_event("example.com", "direct", True, 45)
    tel.record_event("bad.test", "failed", False, 900, "TimeoutError")
    tel.record_event("roblox.com", "tls_record", True, 210, "DoH")
    tel.on_connection_close()

    snap = tel.snapshot(doh_hits=4, doh_fallbacks=1, cached_hosts=2)
    assert snap["ok"] is True
    assert snap["active_connections"] == 0
    assert snap["total_connections"] == 1
    assert snap["bytes_up"] == 1000
    assert snap["bytes_down"] == 5000
    assert snap["strategy_counts"] == {"direct": 1, "tcp_frag": 1, "tls_record": 1, "tunnel": 0, "failed": 1}
    assert len(snap["recent_events"]) == 3  # bounded by max_events=3
    assert snap["recent_events"][0]["host"] == "roblox.com"
