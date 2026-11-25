"""Async local proxy engine: DoH, TLS/TCP DPI bypass, zero-copy pipe, and server."""
from relay.proxy.doh import DoHResolver
from relay.proxy.dpi import StrategyCache, plan_tcp_fragments, plan_tls_record_fragments
from relay.proxy.pipe import bridge_streams, open_upstream, tune_socket
from relay.proxy.server import ProxyServer, run_proxy_server

__all__ = [
    "DoHResolver",
    "ProxyServer",
    "StrategyCache",
    "bridge_streams",
    "open_upstream",
    "plan_tcp_fragments",
    "plan_tls_record_fragments",
    "run_proxy_server",
    "tune_socket",
]
