"""Pooled Keep-Alive DNS-over-HTTPS (DoH) resolver with TTL cache and circuit breaker."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import socket
import ssl
import time
from typing import Iterable
from urllib.parse import quote

from relay.proxy.pipe import abort_writer

# Direct-IP endpoints prevent ISP DNS poisoning from blocking DoH bootstrap itself.
DOH_ENDPOINTS: tuple[tuple[str, int, str, str], ...] = (
    ("1.1.1.1", 443, "cloudflare-dns.com", "/dns-query?name={name}&type=A"),
    ("8.8.8.8", 443, "dns.google", "/resolve?name={name}&type=A"),
    ("9.9.9.9", 443, "dns.quad9.net", "/dns-query?name={name}&type=A"),
)

DEFAULT_PREWARM_HOSTS: tuple[str, ...] = (
    "discord.com",
    "cdn.discordapp.com",
    "gateway.discord.gg",
    "www.roblox.com",
    "tr.rbxcdn.com",
    "assetdelivery.roblox.com",
    "www.wattpad.com",
    "pastebin.com",
)


def is_ip_literal(host: str) -> bool:
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        return False


class DoHResolver:
    """Async DoH resolver with Keep-Alive TLS pooling, in-flight deduplication, and backoff."""

    def __init__(
        self,
        enabled: bool = True,
        timeout: float = 1.8,
        max_pool: int = 6,
        max_entries: int = 4096,
    ):
        self.enabled = enabled
        self.timeout = timeout
        self.max_pool = max_pool
        self.max_entries = max_entries
        self._cache: dict[str, tuple[str | None, float]] = {}
        self._inflight: dict[str, asyncio.Task[str | None]] = {}
        self._pool: list[tuple[asyncio.StreamReader, asyncio.StreamWriter]] = []
        self._ssl_context = ssl.create_default_context()
        self._consecutive_failures = 0
        self._backoff_until = 0.0
        self.hits = 0
        self.fallbacks = 0

    def _cache_set(self, key: str, value: str | None, expires_at: float) -> None:
        now = time.monotonic()
        if len(self._cache) >= self.max_entries and key not in self._cache:
            expired = [k for k, (_, exp) in self._cache.items() if exp <= now]
            for k in expired:
                self._cache.pop(k, None)
            if len(self._cache) >= self.max_entries:
                # Evict oldest inserted quarter (dict preserves insertion order).
                evict_count = max(1, self.max_entries // 4)
                for k in list(self._cache.keys())[:evict_count]:
                    self._cache.pop(k, None)
        self._cache[key] = (value, expires_at)

    def flush(self) -> None:
        """Clear cached DNS entries, reset circuit breaker, and drop pooled sockets."""
        self._cache.clear()
        self._consecutive_failures = 0
        self._backoff_until = 0.0
        self.close()

    async def prewarm(self, hosts: Iterable[str] = DEFAULT_PREWARM_HOSTS) -> None:
        """Warm the primary Keep-Alive TLS pipe and hot blocked domain records."""
        if not self.enabled:
            return
        host_list = list(hosts)
        if not host_list:
            return
        with contextlib.suppress(Exception):
            await self.resolve(host_list[0])
            await asyncio.gather(*(self.resolve(h) for h in host_list[1:]), return_exceptions=True)

    async def resolve(self, hostname: str) -> str | None:
        if not self.enabled or is_ip_literal(hostname) or "." not in hostname:
            return None
        key = hostname.lower().rstrip(".")
        if key == "localhost" or key.endswith(".local") or key.endswith(".lan"):
            return None

        now = time.monotonic()
        cached = self._cache.get(key)
        if cached and cached[1] > now:
            if cached[0] is not None:
                self.hits += 1
            return cached[0]

        if now < self._backoff_until:
            self.fallbacks += 1
            return None

        task = self._inflight.get(key)
        if task is None or task.done():
            task = asyncio.create_task(self._resolve_uncached(key))
            self._inflight[key] = task
            task.add_done_callback(
                lambda done_t, k=key: self._inflight.pop(k, None) if self._inflight.get(k) is done_t else None
            )
        return await asyncio.shield(task)

    async def _resolve_uncached(self, key: str) -> str | None:
        now = time.monotonic()
        try:
            ip, ttl, reached = await self._query_doh(key)
            now = time.monotonic()
            if ip:
                self._consecutive_failures = 0
                self._cache_set(key, ip, now + max(60.0, min(float(ttl), 600.0)))
                self.hits += 1
                return ip
            if reached:
                # Valid DoH response with no IPv4 A record (NXDOMAIN / NODATA / IPv6-only).
                # Do NOT trip the circuit breaker; negative-cache briefly instead.
                self._consecutive_failures = 0
                self._cache_set(key, None, now + max(30.0, min(float(ttl or 60), 120.0)))
                return None
            self._record_failure()
            return None
        except asyncio.CancelledError:
            raise
        except Exception:
            self._record_failure()
            return None

    def close(self) -> None:
        for task in list(self._inflight.values()):
            if not task.done():
                task.cancel()
        self._inflight.clear()
        while self._pool:
            _, writer = self._pool.pop()
            abort_writer(writer)

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= 3:
            self._backoff_until = time.monotonic() + 60.0
        self.fallbacks += 1

    async def _borrow_primary(self) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        while self._pool:
            reader, writer = self._pool.pop()
            if not writer.is_closing() and not reader.at_eof():
                return reader, writer
            writer.close()
        server_ip, server_port, sni_host, _ = DOH_ENDPOINTS[0]
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(
                server_ip,
                server_port,
                ssl=self._ssl_context,
                server_hostname=sni_host,
            ),
            timeout=self.timeout,
        )
        sock = writer.get_extra_info("socket")
        if sock is not None:
            with contextlib.suppress(OSError):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return reader, writer

    def _return_primary(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if len(self._pool) < self.max_pool and not writer.is_closing() and not reader.at_eof():
            self._pool.append((reader, writer))
        else:
            writer.close()

    async def _read_http_body(self, reader: asyncio.StreamReader) -> tuple[bytes, bool, int]:
        header_bytes = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=self.timeout)
        first_line = header_bytes.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
        parts = first_line.split(" ", 2)
        status_code = int(parts[1]) if len(parts) >= 2 and parts[1].isdigit() else 0
        headers_lower = header_bytes.lower()
        keep_alive = b"connection: close" not in headers_lower
        if b"transfer-encoding: chunked" in headers_lower:
            out = bytearray()
            while True:
                line = await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=self.timeout)
                size = int(line.split(b";", 1)[0].strip(), 16)
                if size == 0:
                    await asyncio.wait_for(reader.readuntil(b"\r\n"), timeout=self.timeout)
                    break
                out.extend(await asyncio.wait_for(reader.readexactly(size), timeout=self.timeout))
                await asyncio.wait_for(reader.readexactly(2), timeout=self.timeout)
            return bytes(out), keep_alive, status_code
        content_length = 0
        for line in header_bytes.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                content_length = int(line.split(b":", 1)[1].strip())
                break
        if content_length > 0:
            body = await asyncio.wait_for(reader.readexactly(content_length), timeout=self.timeout)
            return body, keep_alive, status_code
        body = await asyncio.wait_for(reader.read(16384), timeout=self.timeout)
        return body, False, status_code

    @staticmethod
    def _extract_a_record(body: bytes) -> tuple[str | None, int, bool]:
        """Parse DoH JSON body -> (ipv4_or_none, ttl, is_valid_dns_response)."""
        payload = json.loads(body.decode("utf-8", errors="replace"))
        if not isinstance(payload, dict) or "Status" not in payload:
            return None, 0, False
        for answer in payload.get("Answer", ()):
            if answer.get("type") == 1 and answer.get("data"):
                ip = str(answer["data"]).strip()
                if is_ip_literal(ip):
                    return ip, int(answer.get("TTL", 120)), True
        # Valid DNS response with no A record (e.g. Status=3 NXDOMAIN or IPv6-only NODATA).
        return None, 60, True

    async def _query_doh(self, hostname: str) -> tuple[str | None, int, bool]:
        encoded = quote(hostname, safe=".-")
        _, _, primary_sni, primary_tpl = DOH_ENDPOINTS[0]

        for _ in range(2):
            writer = None
            try:
                reader, writer = await self._borrow_primary()
                path = primary_tpl.format(name=encoded)
                req = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {primary_sni}\r\n"
                    f"Accept: application/dns-json\r\n"
                    f"Connection: keep-alive\r\n\r\n"
                ).encode("ascii")
                writer.write(req)
                await writer.drain()
                body, keep_alive, status_code = await self._read_http_body(reader)
                if keep_alive and status_code == 200:
                    self._return_primary(reader, writer)
                else:
                    writer.close()
                if status_code != 200:
                    continue
                ip, ttl, valid = self._extract_a_record(body)
                if valid:
                    return ip, ttl, True
                break
            except asyncio.CancelledError:
                if writer is not None:
                    abort_writer(writer)
                raise
            except Exception:
                if writer is not None:
                    abort_writer(writer)
                continue

        for server_ip, server_port, sni_host, path_tpl in DOH_ENDPOINTS[1:]:
            writer = None
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(
                        server_ip,
                        server_port,
                        ssl=self._ssl_context,
                        server_hostname=sni_host,
                    ),
                    timeout=self.timeout,
                )
                path = path_tpl.format(name=encoded)
                req = (
                    f"GET {path} HTTP/1.1\r\n"
                    f"Host: {sni_host}\r\n"
                    f"Accept: application/dns-json\r\n"
                    f"Connection: close\r\n\r\n"
                ).encode("ascii")
                writer.write(req)
                await writer.drain()
                body, _, status_code = await self._read_http_body(reader)
                if status_code != 200:
                    continue
                ip, ttl, valid = self._extract_a_record(body)
                if valid:
                    return ip, ttl, True
            except asyncio.CancelledError:
                raise
            except Exception:
                continue
            finally:
                if writer is not None:
                    abort_writer(writer)
        return None, 0, False
