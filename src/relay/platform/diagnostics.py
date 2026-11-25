"""Parallel endpoint reachability and latency diagnostics."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import contextlib
from dataclasses import asdict, dataclass
import http.client
import socket
import ssl
import time

DIAGNOSTIC_TARGETS: tuple[tuple[str, str, str, str | None], ...] = (
    ("Discord", "discord.com", "/", None),
    ("Roblox", "www.roblox.com", "/", None),
    ("Steam Store", "store.steampowered.com", "/", None),
    ("Epic Games", "account-public-service-prod.ol.epicgames.com", "/account/api/version", None),
    ("Cloudflare DoH", "1.1.1.1", "/dns-query?name=example.com&type=A", "cloudflare-dns.com"),
)


@dataclass(frozen=True)
class DiagnosticResult:
    name: str
    host: str
    ok: bool
    status: int
    latency_ms: int
    error: str = ""

    def to_dict(self) -> dict:
        return asdict(self)


class _SniHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection allowing explicit TLS SNI hostname (e.g. cloudflare-dns.com -> 1.1.1.1)."""

    def __init__(self, *args, sni_hostname: str | None = None, **kwargs):
        super().__init__(*args, **kwargs)
        self._sni_hostname = sni_hostname

    def connect(self) -> None:
        if not self._sni_hostname:
            super().connect()
            return
        sock = self._create_connection(
            (self.host, self.port),
            self.timeout,
            self.source_address,
        )
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
        self.sock = self._context.wrap_socket(sock, server_hostname=self._sni_hostname)


def _format_probe_error(exc: Exception) -> str:
    msg = str(exc)
    lower = msg.lower()
    winerror = getattr(exc, "winerror", None)
    if winerror in (10054, 1236) or "10054" in msg or "1236" in msg:
        return "Bağlantı sıfırlandı (DPI/RST)"
    if isinstance(exc, (socket.timeout, TimeoutError)) or "timed out" in lower:
        return "Zaman aşımı (DPI/Ağ engeli)"
    if "unexpected_eof" in lower:
        return "TLS el sıkışması kesildi"
    return msg[:60] or type(exc).__name__


def _probe_single(
    name: str,
    host: str,
    path: str,
    proxy_port: int | None,
    sni_host: str | None = None,
) -> DiagnosticResult:
    started = time.monotonic()
    ctx = ssl.create_default_context()
    conn: http.client.HTTPSConnection | None = None
    try:
        if proxy_port:
            conn = _SniHTTPSConnection("127.0.0.1", proxy_port, timeout=8, context=ctx, sni_hostname=sni_host)
            conn.set_tunnel(host, 443)
        else:
            conn = _SniHTTPSConnection(host, 443, timeout=8, context=ctx, sni_hostname=sni_host)
        headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/131.0.0.0 Safari/537.36",
            "Accept": "application/dns-json" if host == "1.1.1.1" else "*/*",
        }
        if sni_host:
            headers["Host"] = sni_host
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        resp.read(256)
        latency_ms = int((time.monotonic() - started) * 1000)
        ok = 200 <= resp.status < 400
        return DiagnosticResult(
            name=name,
            host=host,
            ok=ok,
            status=resp.status,
            latency_ms=latency_ms,
            error="" if ok else f"HTTP {resp.status}",
        )
    except (OSError, http.client.HTTPException, ssl.SSLError, socket.timeout) as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return DiagnosticResult(
            name=name,
            host=host,
            ok=False,
            status=0,
            latency_ms=latency_ms,
            error=_format_probe_error(exc),
        )
    finally:
        if conn is not None:
            with contextlib.suppress(Exception):
                conn.close()


def run_diagnostics(proxy_port: int | None = None) -> list[dict]:
    with ThreadPoolExecutor(max_workers=len(DIAGNOSTIC_TARGETS)) as pool:
        futures = [
            pool.submit(_probe_single, name, host, path, proxy_port, sni_host)
            for name, host, path, sni_host in DIAGNOSTIC_TARGETS
        ]
        return [f.result().to_dict() for f in futures]
