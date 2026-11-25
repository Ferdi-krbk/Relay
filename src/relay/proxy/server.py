"""Async local HTTPS CONNECT + HTTP proxy server with management endpoints."""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import os
from pathlib import Path
import socket
import time
from urllib.parse import urlsplit

from relay.core.config import (
    MODE_PRESETS,
    STRICT_TUNNEL_DOMAINS,
    TUNNEL_PORT,
    NetworkMode,
    default_data_dir,
    domain_matches,
    parse_network_mode,
)
from relay.core.pac import generate_pac
from relay.core.telemetry import ProxyTelemetry
from relay.proxy.doh import DoHResolver
from relay.proxy.dpi import StrategyCache, exchange_hello, race_client_hello, read_client_hello
from relay.proxy.pipe import STREAM_LIMIT_BYTES, bridge_streams, open_upstream, tune_socket
from relay.proxy.tunnel import MasqueTunnel, open_socks5_upstream


def _detect_local_ip() -> str:
    """Zero-packet kernel routing table lookup for the active outbound local IP."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("1.1.1.1", 80))
            return str(s.getsockname()[0])
    except OSError:
        return ""


def _parse_headers(request: bytes) -> dict[str, str]:
    headers: dict[str, str] = {}
    for line in request.split(b"\r\n")[1:]:
        if not line or b":" not in line:
            continue
        k, v = line.split(b":", 1)
        headers[k.decode("ascii", errors="replace").strip().lower()] = v.decode("ascii", errors="replace").strip()
    return headers


def install_loop_exception_handler(loop: asyncio.AbstractEventLoop) -> None:
    """Suppress harmless Windows ProactorEventLoop socket reset noise on abrupt client closes."""
    default_handler = loop.get_exception_handler()

    def _handler(current_loop: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        if isinstance(exc, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return
        if default_handler:
            default_handler(current_loop, context)
        else:
            current_loop.default_exception_handler(context)

    loop.set_exception_handler(_handler)


class ProxyServer:
    """Self-contained async HTTP/HTTPS/WSS proxy server."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = 11893,
        mode: NetworkMode = NetworkMode.AUTO,
        delay: float | None = None,
        race_delay: float | None = None,
        prefer_tls_records: bool = False,
        doh_enabled: bool = True,
        bypass_domains: tuple[str, ...] = (),
        force_fragment_domains: tuple[str, ...] = (),
        prewarm_doh: bool = True,
        control_token: str | None = None,
        learned_path: Path | None = None,
        tunnel_host: str = "127.0.0.1",
        tunnel_port: int | None = None,
        managed_tunnel: MasqueTunnel | None = None,
    ):
        preset = MODE_PRESETS[mode]
        self.host = host
        self.port = port
        self.mode = mode
        self.delay = preset.delay if delay is None else delay
        self.race_delay = preset.race_delay if race_delay is None else race_delay
        self.prefer_tls_records = bool(prefer_tls_records or preset.prefer_tls_records)
        self.race_both = preset.race_both
        self.bypass_domains = tuple(sorted({d.lower().strip(".") for d in bypass_domains if d.strip()}))
        self.force_fragment_domains = tuple(
            sorted({d.lower().strip(".") for d in force_fragment_domains if d.strip()})
        )
        self.prewarm_doh = prewarm_doh
        self.control_token = control_token or None
        self.tunnel_host = tunnel_host
        self.tunnel_port = tunnel_port
        self._managed_tunnel = managed_tunnel

        self.resolver = DoHResolver(enabled=doh_enabled)
        self.strategies = StrategyCache(persist_path=learned_path)
        self._doh_hosts: set[str] = set()
        self._tunnel_roots: set[str] = set()
        self.telemetry = ProxyTelemetry(mode=mode.value, doh_enabled=doh_enabled)
        self.shutdown_event = asyncio.Event()
        self._server: asyncio.Server | None = None

    @staticmethod
    def _root_domain(hostname: str) -> str:
        parts = hostname.lower().rstrip(".").split(".")
        return ".".join(parts[-2:]) if len(parts) >= 2 else hostname.lower().rstrip(".")

    def _should_use_tunnel_directly(self, hostname: str, cached: str | None) -> bool:
        if self.tunnel_port is None:
            return False
        if cached == "tunnel":
            return True
        if self._root_domain(hostname) in self._tunnel_roots:
            return True
        if self.mode == NetworkMode.STRICT and domain_matches(hostname, STRICT_TUNNEL_DOMAINS):
            return True
        return False

    def _record_tunnel_host(self, hostname: str) -> None:
        self.strategies.set(hostname, "tunnel", persist=False)
        root = self._root_domain(hostname)
        if root:
            self._tunnel_roots.add(root)
            if root == "roblox.com":
                self._tunnel_roots.add("rbxcdn.com")
            elif root == "rbxcdn.com":
                self._tunnel_roots.add("roblox.com")

    async def _start_managed_tunnel(self) -> None:
        if self._managed_tunnel is None:
            return
        ok = await asyncio.to_thread(self._managed_tunnel.start)
        if ok:
            self.tunnel_port = self._managed_tunnel.port
            print(
                f"Relay MASQUE tunnel ready: socks5://{self._managed_tunnel.host}:{self._managed_tunnel.port}",
                flush=True,
            )

    async def _open_tunnel(self, hostname: str, port: int) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
        if self.tunnel_port is None:
            raise ConnectionError("Tunnel fallback is not active")
        return await open_socks5_upstream(self.tunnel_host, self.tunnel_port, hostname, port, timeout=6.0)

    def flush_caches(self, *, clear_persisted: bool = False) -> None:
        """Reset strategy cache, DoH-required host set, and pooled DoH sockets."""
        self.strategies.clear(include_persisted=clear_persisted)
        self._doh_hosts.clear()
        self._tunnel_roots.clear()
        self.resolver.flush()

    def apply_live_config(self, payload: dict) -> dict:
        """Hot-reload proxy mode, DoH flag, and domain rules without dropping active tunnels."""
        if isinstance(payload.get("mode"), str):
            new_mode = parse_network_mode(payload["mode"], default=self.mode)
            preset = MODE_PRESETS[new_mode]
            self.mode = new_mode
            self.delay = preset.delay
            self.race_delay = preset.race_delay
            self.prefer_tls_records = preset.prefer_tls_records
            self.race_both = preset.race_both
            self.telemetry.mode = new_mode.value
        if isinstance(payload.get("doh_enabled"), bool):
            was_enabled = self.resolver.enabled
            self.resolver.enabled = payload["doh_enabled"]
            self.telemetry.doh_enabled = payload["doh_enabled"]
            if not payload["doh_enabled"]:
                self.resolver.close()
            elif not was_enabled and self.prewarm_doh:
                with contextlib.suppress(RuntimeError):
                    asyncio.create_task(self.resolver.prewarm())
        if isinstance(payload.get("bypass_domains"), list):
            self.bypass_domains = tuple(
                sorted({str(d).lower().strip(".") for d in payload["bypass_domains"] if str(d).strip()})
            )
        if isinstance(payload.get("force_fragment_domains"), list):
            self.force_fragment_domains = tuple(
                sorted({str(d).lower().strip(".") for d in payload["force_fragment_domains"] if str(d).strip()})
            )
        return {
            "ok": True,
            "mode": self.mode.value,
            "doh": self.resolver.enabled,
            "bypass_count": len(self.bypass_domains),
            "force_count": len(self.force_fragment_domains),
        }

    async def _network_monitor(self) -> None:
        """Detect local IP changes (Wi-Fi roaming) or system wake-from-sleep and flush stale caches."""
        last_ip = _detect_local_ip()
        last_mono = time.monotonic()
        last_wall = time.time()
        while not self.shutdown_event.is_set():
            await asyncio.sleep(5.0)
            now_mono = time.monotonic()
            now_wall = time.time()
            wall_drift = abs((now_wall - last_wall) - (now_mono - last_mono))
            cur_ip = _detect_local_ip()
            if wall_drift > 15.0 or (cur_ip and last_ip and cur_ip != last_ip):
                self.flush_caches(clear_persisted=False)
                if self.prewarm_doh and self.resolver.enabled:
                    asyncio.create_task(self.resolver.prewarm())
            if (
                self._managed_tunnel is not None
                and not self._managed_tunnel.available
                and not self._managed_tunnel.is_ready()
            ):
                if await asyncio.to_thread(self._managed_tunnel.start):
                    self.tunnel_port = self._managed_tunnel.port
            if cur_ip:
                last_ip = cur_ip
            last_mono = now_mono
            last_wall = now_wall

    @property
    def bound_port(self) -> int:
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        install_loop_exception_handler(loop)
        self._server = await asyncio.start_server(
            self.handle_client,
            self.host,
            self.port,
            limit=STREAM_LIMIT_BYTES,
        )

    async def serve(self) -> None:
        if self._server is None:
            await self.start()
        assert self._server is not None
        print(
            f"Relay proxy ready: http://{self.host}:{self.bound_port} "
            f"(mode={self.mode.value}, doh={self.resolver.enabled})",
            flush=True,
        )
        warmup_task = asyncio.create_task(self.resolver.prewarm()) if self.prewarm_doh else None
        tunnel_task = asyncio.create_task(self._start_managed_tunnel()) if self._managed_tunnel is not None else None
        monitor_task = asyncio.create_task(self._network_monitor())
        try:
            async with self._server:
                serve_task = asyncio.create_task(self._server.serve_forever())
                stop_task = asyncio.create_task(self.shutdown_event.wait())
                _, pending = await asyncio.wait({serve_task, stop_task}, return_when=asyncio.FIRST_COMPLETED)
                for task in pending:
                    task.cancel()
                await asyncio.gather(*pending, return_exceptions=True)
        finally:
            for bg in (warmup_task, tunnel_task, monitor_task):
                if bg is not None:
                    bg.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await bg
            if self._managed_tunnel is not None:
                self._managed_tunnel.stop()
            self.resolver.close()

    async def stop(self) -> None:
        self.shutdown_event.set()
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await self._server.wait_closed()
        if self._managed_tunnel is not None:
            self._managed_tunnel.stop()
        self.resolver.close()

    @staticmethod
    async def _send_json(writer: asyncio.StreamWriter, payload: dict, status: str = "200 OK") -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        header = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: application/json; charset=utf-8\r\n"
            f"Content-Length: {len(body)}\r\n"
            f"Cache-Control: no-store\r\n"
            f"Connection: close\r\n\r\n"
        ).encode("ascii")
        writer.write(header + body)
        await writer.drain()

    def _is_control_authorized(self, headers: dict[str, str]) -> bool:
        """Reject browser-originated requests (CSRF/no-cors) and validate X-Relay-Token when configured."""
        if "origin" in headers or "sec-fetch-site" in headers:
            return False
        if self.control_token is not None:
            supplied = headers.get("x-relay-token", "")
            if not supplied or not hmac.compare_digest(supplied, self.control_token):
                return False
        return True

    async def _handle_control(
        self,
        method: str,
        authority: str,
        request: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> bool:
        if method == "GET" and authority == "/health":
            await self._send_json(
                writer,
                {
                    "ok": True,
                    "pid": os.getpid(),
                    "mode": self.mode.value,
                    "doh": self.resolver.enabled,
                    "tunnel": self.tunnel_port is not None,
                    "uptime": int(time.monotonic() - self.telemetry.started_at),
                },
            )
            return True

        if method == "GET" and authority == "/stats":
            await self._send_json(
                writer,
                self.telemetry.snapshot(
                    doh_hits=self.resolver.hits,
                    doh_fallbacks=self.resolver.fallbacks,
                    cached_hosts=len(self.strategies),
                ),
            )
            return True

        if method == "GET" and authority == "/proxy.pac":
            port = writer.get_extra_info("sockname")[1]
            body = generate_pac(port, self.bypass_domains)
            header = (
                f"HTTP/1.1 200 OK\r\n"
                f"Content-Type: application/x-ns-proxy-autoconfig\r\n"
                f"Content-Length: {len(body)}\r\n"
                f"Cache-Control: no-store\r\n"
                f"Connection: close\r\n\r\n"
            ).encode("ascii")
            writer.write(header + body)
            await writer.drain()
            return True

        if method == "POST" and authority in ("/shutdown", "/config", "/flush"):
            headers = _parse_headers(request)
            if not self._is_control_authorized(headers):
                await self._send_json(writer, {"ok": False, "error": "forbidden"}, status="403 Forbidden")
                return True

            body_bytes = b""
            content_len = int(headers.get("content-length", "0") or "0")
            if 0 < content_len <= 131072:
                body_bytes = await asyncio.wait_for(reader.readexactly(content_len), timeout=3.0)

            if authority == "/shutdown":
                await self._send_json(writer, {"ok": True, "stopping": True})
                self.shutdown_event.set()
                return True

            if authority == "/flush":
                clear_persisted = False
                if body_bytes:
                    with contextlib.suppress(ValueError):
                        clear_persisted = bool(json.loads(body_bytes.decode("utf-8")).get("clear_persisted", False))
                self.flush_caches(clear_persisted=clear_persisted)
                await self._send_json(writer, {"ok": True, "flushed": True})
                return True

            if authority == "/config":
                try:
                    payload = json.loads(body_bytes.decode("utf-8")) if body_bytes else {}
                except ValueError:
                    await self._send_json(writer, {"ok": False, "error": "invalid json"}, status="400 Bad Request")
                    return True
                updated = self.apply_live_config(payload if isinstance(payload, dict) else {})
                await self._send_json(writer, updated)
                return True

        return False

    async def _handle_plain_http(
        self,
        method: str,
        authority: str,
        version: str,
        request: bytes,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        started: float,
    ) -> asyncio.StreamWriter | None:
        parsed = urlsplit(authority)
        if not parsed.hostname:
            writer.write(b"HTTP/1.1 400 Bad Request\r\nConnection: close\r\n\r\n")
            await writer.drain()
            return None

        hostname = parsed.hostname.lower()
        port = parsed.port or 80
        rel_path = parsed.path or "/"
        if parsed.query:
            rel_path += "?" + parsed.query

        tune_socket(writer)
        self.telemetry.on_connection_open()
        upstream = None
        try:
            use_doh = domain_matches(hostname, self.force_fragment_domains)
            resolved_ip = await self.resolver.resolve(hostname) if use_doh else None
            target = resolved_ip or hostname
            try:
                upstream_reader, upstream = await open_upstream(target, port)
            except OSError:
                if not resolved_ip and self.resolver.enabled:
                    resolved_ip = await self.resolver.resolve(hostname)
                    if resolved_ip:
                        upstream_reader, upstream = await open_upstream(resolved_ip, port)
                    else:
                        raise
                elif resolved_ip:
                    upstream_reader, upstream = await open_upstream(hostname, port)
                else:
                    raise

            lines = request.split(b"\r\n")
            new_first = f"{method} {rel_path} {version}".encode("ascii")
            header_lines = [
                ln
                for ln in lines[1:]
                if ln
                and not ln.lower().startswith(b"proxy-connection:")
                and not ln.lower().startswith(b"connection:")
            ]
            rebuilt = b"\r\n".join([new_first, *header_lines, b"Connection: close", b"", b""])
            upstream.write(rebuilt)
            await upstream.drain()
            latency_ms = int((time.monotonic() - started) * 1000)
            self.telemetry.record_event(hostname, "direct", True, latency_ms, "http")
            await bridge_streams(reader, writer, upstream_reader, upstream, self.telemetry)
            return upstream
        finally:
            self.telemetry.on_connection_close()

    async def handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        upstream: asyncio.StreamWriter | None = None
        connected = False
        counted = False
        hostname = ""
        started = time.monotonic()

        try:
            request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10.0)
            first_line = request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
            parts = first_line.split(" ", 2)
            if len(parts) < 3:
                return
            method, authority, version = parts

            if await self._handle_control(method, authority, request, reader, writer):
                return

            if method != "CONNECT" and authority.lower().startswith("http://"):
                upstream = await self._handle_plain_http(
                    method, authority, version, request, reader, writer, started
                )
                return

            if method != "CONNECT":
                writer.write(b"HTTP/1.1 405 Method Not Allowed\r\nConnection: close\r\n\r\n")
                await writer.drain()
                return

            host, port_text = authority.rsplit(":", 1)
            hostname = host.strip("[]").lower()
            port = int(port_text)

            tune_socket(writer)
            self.telemetry.on_connection_open()
            counted = True

            cached = self.strategies.get(hostname)
            force_frag = domain_matches(hostname, self.force_fragment_domains)
            via_tunnel = False
            resolved_ip: str | None = None
            target_host = hostname
            hello_timeout = 2.5 if self.tunnel_port is not None else 4.0

            if self._should_use_tunnel_directly(hostname, cached):
                try:
                    upstream_reader, upstream = await self._open_tunnel(hostname, port)
                    via_tunnel = True
                except (OSError, asyncio.TimeoutError):
                    self.strategies.invalidate(hostname)
                    cached = None

            if not via_tunnel:
                # Smart DNS: Normal unblocked hosts use instant system DNS (local CDN edge);
                # DoH is used upfront only for forced domains or hosts that previously needed DoH.
                use_doh_first = force_frag or (hostname in self._doh_hosts)
                resolved_ip = await self.resolver.resolve(hostname) if use_doh_first else None
                target_host = resolved_ip or hostname

                try:
                    upstream_reader, upstream = await open_upstream(target_host, port)
                except (OSError, asyncio.TimeoutError):
                    opened = False
                    if not resolved_ip and self.resolver.enabled:
                        resolved_ip = await self.resolver.resolve(hostname)
                        if resolved_ip:
                            target_host = resolved_ip
                            with contextlib.suppress(OSError, asyncio.TimeoutError):
                                upstream_reader, upstream = await open_upstream(target_host, port)
                                self._doh_hosts.add(hostname)
                                opened = True
                    elif resolved_ip:
                        target_host = hostname
                        with contextlib.suppress(OSError, asyncio.TimeoutError):
                            upstream_reader, upstream = await open_upstream(hostname, port)
                            opened = True
                    if not opened:
                        if self.tunnel_port is not None:
                            upstream_reader, upstream = await self._open_tunnel(hostname, port)
                            via_tunnel = True
                            self._record_tunnel_host(hostname)
                        else:
                            raise

            writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
            await writer.drain()
            connected = True

            peername = upstream.get_extra_info("peername")
            upstream_ip = str(peername[0]) if isinstance(peername, tuple) and peername else target_host

            hello, is_tls = await read_client_hello(reader)
            if is_tls:
                if via_tunnel:
                    upstream_reader, upstream, first_response = await exchange_hello(
                        upstream_reader, upstream, hello, hostname, 0.0, "direct", timeout=6.0
                    )
                    used_strategy = "tunnel"
                else:
                    used_strategy = cached or "direct"
                    tunnel_cb = (
                        (lambda h=hostname, p=port: self._open_tunnel(h, p))
                        if self.tunnel_port is not None
                        else None
                    )
                    try:
                        if cached:
                            try:
                                upstream_reader, upstream, first_response = await exchange_hello(
                                    upstream_reader, upstream, hello, hostname, self.delay, cached, timeout=hello_timeout
                                )
                            except (OSError, asyncio.TimeoutError):
                                self.strategies.invalidate(hostname)
                                if not resolved_ip and self.resolver.enabled:
                                    resolved_ip = await self.resolver.resolve(hostname)
                                    if resolved_ip:
                                        target_host = resolved_ip
                                upstream_reader, upstream = await open_upstream(target_host, port)
                                (upstream_reader, upstream, first_response), used_strategy = await race_client_hello(
                                    upstream_reader,
                                    upstream,
                                    hello,
                                    hostname,
                                    target_host,
                                    port,
                                    delay=self.delay,
                                    race_delay=self.race_delay,
                                    prefer_tls_records=self.prefer_tls_records,
                                    race_both=self.race_both,
                                    force_fragment=force_frag,
                                    hello_timeout=hello_timeout,
                                    tunnel_connect=tunnel_cb,
                                )
                                if used_strategy == "tunnel":
                                    self._record_tunnel_host(hostname)
                                else:
                                    if resolved_ip and used_strategy != "direct":
                                        self._doh_hosts.add(hostname)
                                    self.strategies.set(
                                        hostname,
                                        used_strategy,
                                        persist=(used_strategy != "direct"),
                                    )
                        else:
                            try:
                                (upstream_reader, upstream, first_response), used_strategy = await race_client_hello(
                                    upstream_reader,
                                    upstream,
                                    hello,
                                    hostname,
                                    target_host,
                                    port,
                                    delay=self.delay,
                                    race_delay=self.race_delay,
                                    prefer_tls_records=self.prefer_tls_records,
                                    race_both=self.race_both,
                                    force_fragment=force_frag,
                                    hello_timeout=hello_timeout,
                                    tunnel_connect=tunnel_cb,
                                )
                            except (OSError, asyncio.TimeoutError):
                                if not resolved_ip and self.resolver.enabled:
                                    resolved_ip = await self.resolver.resolve(hostname)
                                    if resolved_ip and resolved_ip != upstream_ip:
                                        target_host = resolved_ip
                                        upstream_reader, upstream = await open_upstream(target_host, port)
                                        (upstream_reader, upstream, first_response), used_strategy = (
                                            await race_client_hello(
                                                upstream_reader,
                                                upstream,
                                                hello,
                                                hostname,
                                                target_host,
                                                port,
                                                delay=self.delay,
                                                race_delay=self.race_delay,
                                                prefer_tls_records=self.prefer_tls_records,
                                                race_both=self.race_both,
                                                force_fragment=True,
                                                hello_timeout=hello_timeout,
                                                tunnel_connect=tunnel_cb,
                                            )
                                        )
                                        self._doh_hosts.add(hostname)
                                    else:
                                        raise
                                else:
                                    raise
                            if used_strategy == "tunnel":
                                self._record_tunnel_host(hostname)
                            else:
                                self.strategies.set(
                                    hostname,
                                    used_strategy,
                                    persist=(
                                        used_strategy != "direct"
                                        and (force_frag or hostname in self._doh_hosts)
                                    ),
                                )
                    except (OSError, asyncio.TimeoutError):
                        if self.tunnel_port is None:
                            raise
                        upstream_reader, upstream = await self._open_tunnel(hostname, port)
                        upstream_reader, upstream, first_response = await exchange_hello(
                            upstream_reader, upstream, hello, hostname, 0.0, "direct", timeout=6.0
                        )
                        used_strategy = "tunnel"
                        self._record_tunnel_host(hostname)

                latency_ms = int((time.monotonic() - started) * 1000)
                note = "MASQUE" if used_strategy == "tunnel" else ("DoH" if resolved_ip else "")
                self.telemetry.record_event(hostname, used_strategy, True, latency_ms, note)
                self.telemetry.add_bytes(len(first_response), is_upload=False)
                writer.write(first_response)
                await writer.drain()
            else:
                upstream.write(hello)
                await upstream.drain()
                latency_ms = int((time.monotonic() - started) * 1000)
                self.telemetry.record_event(
                    hostname,
                    "tunnel" if via_tunnel else "direct",
                    True,
                    latency_ms,
                    "MASQUE" if via_tunnel else "plain",
                )

            await bridge_streams(reader, writer, upstream_reader, upstream, self.telemetry)
        except (
            OSError,
            ValueError,
            OverflowError,
            UnicodeError,
            asyncio.TimeoutError,
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
        ) as exc:
            # Browsers frequently open speculative preconnect tunnels and close unused ones
            # with 0 bytes sent before ClientHello; do not pollute telemetry as failures.
            benign_preconnect_close = (
                connected and isinstance(exc, asyncio.IncompleteReadError) and not exc.partial
            )
            if hostname and not benign_preconnect_close:
                latency_ms = int((time.monotonic() - started) * 1000)
                self.telemetry.record_event(hostname, "failed", False, latency_ms, type(exc).__name__)
            if not connected:
                with contextlib.suppress(OSError):
                    writer.write(b"HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\n\r\n")
                    await writer.drain()
        finally:
            if counted:
                self.telemetry.on_connection_close()
            for stream in (writer, upstream):
                if stream is not None:
                    stream.close()
                    with contextlib.suppress(OSError):
                        await stream.wait_closed()


async def run_proxy_server(args) -> None:
    bypass = tuple(d for d in (getattr(args, "bypass", "") or "").split(",") if d.strip())
    force = tuple(d for d in (getattr(args, "force_fragment", "") or "").split(",") if d.strip())
    token = getattr(args, "control_token", None) or os.environ.get("RELAY_CONTROL_TOKEN") or None
    learned = default_data_dir() / "learned-strategies.json"
    managed_tunnel = None if bool(getattr(args, "no_tunnel", False)) else MasqueTunnel(port=TUNNEL_PORT)
    server = ProxyServer(
        port=args.port,
        mode=parse_network_mode(getattr(args, "mode", NetworkMode.AUTO.value)),
        delay=getattr(args, "delay", None),
        race_delay=getattr(args, "race_delay", None),
        prefer_tls_records=bool(getattr(args, "tls_records", False)),
        doh_enabled=not bool(getattr(args, "no_doh", False)),
        bypass_domains=bypass,
        force_fragment_domains=force,
        control_token=token,
        learned_path=learned,
        managed_tunnel=managed_tunnel,
    )
    await server.serve()
