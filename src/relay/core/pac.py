"""Zero-DNS Proxy Auto-Configuration (PAC) generator.

Never emits `isInNet()`, because Windows/Chrome PAC engines trigger a synchronous
system DNS query for `isInNet()`, which stalls or bypasses ISP DNS-blocked domains.
"""
from __future__ import annotations

from typing import Iterable

from relay.core.config import sanitize_domain


def generate_pac(port: int, bypass_domains: Iterable[str] = ()) -> bytes:
    """Build a fast, zero-DNS PAC script routing HTTPS, WSS, and HTTP via 127.0.0.1:port."""
    if not 1 <= port <= 65535:
        raise ValueError(f"Invalid proxy port: {port}")

    cleaned = sorted({d for d in (sanitize_domain(item) for item in bypass_domains) if d})
    bypass_clause = ""
    if cleaned:
        checks = (f'(host === "{domain}" || dnsDomainIs(host, ".{domain}"))' for domain in cleaned)
        bypass_clause = "if (" + " || ".join(checks) + ') return "DIRECT"; '

    script = (
        "function FindProxyForURL(url, host) {"
        'host = host.toLowerCase().replace(/\\.$/, ""); '
        'if (isPlainHostName(host) || host === "localhost" || host === "127.0.0.1" || host === "::1" || host === "[::1]" || '
        '/^(10\\.|127\\.|192\\.168\\.|172\\.(1[6-9]|2\\d|3[01])\\.)/.test(host)) return "DIRECT"; '
        "var proto = url.substring(0, 6).toLowerCase(); "
        'if (proto !== "https:" && proto !== "wss://" && proto.substring(0, 5) !== "http:") return "DIRECT"; '
        + bypass_clause
        + f'return "PROXY 127.0.0.1:{port}; DIRECT";'
        + "}"
    )
    return script.encode("utf-8")
