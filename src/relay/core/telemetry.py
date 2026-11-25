"""Live connection telemetry, throughput sampling, and bounded event log."""
from __future__ import annotations

from collections import deque
from dataclasses import asdict, dataclass
import os
import time


@dataclass(frozen=True)
class ConnectionEvent:
    time: str
    host: str
    strategy: str
    ok: bool
    latency_ms: int
    note: str = ""


class ProxyTelemetry:
    """In-memory runtime metrics and ring-buffered connection activity feed."""

    def __init__(self, mode: str, doh_enabled: bool, max_events: int = 40):
        self.mode = mode
        self.doh_enabled = doh_enabled
        self.started_at = time.monotonic()

        self.active_connections = 0
        self.total_connections = 0
        self.bytes_up = 0
        self.bytes_down = 0
        self.strategy_counts: dict[str, int] = {
            "direct": 0,
            "tcp_frag": 0,
            "tls_record": 0,
            "tunnel": 0,
            "failed": 0,
        }
        self.recent_events: deque[ConnectionEvent] = deque(maxlen=max_events)

        self._last_sample_at = self.started_at
        self._last_up = 0
        self._last_down = 0
        self._bps_up = 0.0
        self._bps_down = 0.0

    def on_connection_open(self) -> None:
        self.active_connections += 1
        self.total_connections += 1

    def on_connection_close(self) -> None:
        self.active_connections = max(0, self.active_connections - 1)

    def add_bytes(self, count: int, *, is_upload: bool) -> None:
        if is_upload:
            self.bytes_up += count
        else:
            self.bytes_down += count

    def record_event(self, host: str, strategy: str, ok: bool, latency_ms: int, note: str = "") -> None:
        if ok and strategy in self.strategy_counts:
            self.strategy_counts[strategy] += 1
        elif not ok:
            self.strategy_counts["failed"] += 1
        self.recent_events.appendleft(
            ConnectionEvent(
                time=time.strftime("%H:%M:%S"),
                host=host,
                strategy=strategy,
                ok=ok,
                latency_ms=max(0, int(latency_ms)),
                note=note,
            )
        )

    def snapshot(self, *, doh_hits: int = 0, doh_fallbacks: int = 0, cached_hosts: int = 0) -> dict:
        now = time.monotonic()
        dt = now - self._last_sample_at
        if dt >= 0.4:
            self._bps_up = max(0.0, (self.bytes_up - self._last_up) / dt)
            self._bps_down = max(0.0, (self.bytes_down - self._last_down) / dt)
            self._last_sample_at = now
            self._last_up = self.bytes_up
            self._last_down = self.bytes_down
        return {
            "ok": True,
            "pid": os.getpid(),
            "mode": self.mode,
            "doh": self.doh_enabled,
            "uptime": int(now - self.started_at),
            "active_connections": self.active_connections,
            "total_connections": self.total_connections,
            "bytes_up": self.bytes_up,
            "bytes_down": self.bytes_down,
            "bps_up": round(self._bps_up, 1),
            "bps_down": round(self._bps_down, 1),
            "strategy_counts": dict(self.strategy_counts),
            "doh_hits": doh_hits,
            "doh_fallbacks": doh_fallbacks,
            "cached_hosts": cached_hosts,
            "recent_events": [asdict(ev) for ev in list(self.recent_events)[:25]],
        }
