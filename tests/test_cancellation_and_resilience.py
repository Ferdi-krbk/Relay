"""Regression and stress tests for async cancellation, DoH deduplication, and tunnel resilience."""
from __future__ import annotations

import asyncio
import struct
import time

from relay.core.config import NetworkMode
from relay.proxy.doh import DoHResolver
from relay.proxy.dpi import race_client_hello
from relay.proxy.server import ProxyServer


def _build_dummy_client_hello(hostname: str = "www.wattpad.com") -> bytes:
    host_b = hostname.encode("ascii")
    # Minimal syntactically valid TLS 1.2 ClientHello record (0x16 0x03 0x01) containing SNI bytes
    body = b"\x01\x00\x00" + bytes([len(host_b) + 40]) + (b"\x03\x03" + b"\x00" * 34 + host_b + b"\x00" * 4)
    return b"\x16\x03\x01" + len(body).to_bytes(2, "big") + body


def test_doh_inflight_caller_cancellation_does_not_deadlock_or_corrupt_peer(monkeypatch):
    """Cancelling either the first or second deduplicated DoH caller must never hang or break peers."""

    async def _run():
        resolver = DoHResolver(enabled=True)

        async def _slow_query(host: str):
            await asyncio.sleep(0.12)
            return "52.84.150.46", 120, True

        monkeypatch.setattr(resolver, "_query_doh", _slow_query)

        # Case 1: Caller 1 is cancelled mid-flight; Caller 2 must still resolve cleanly.
        t1 = asyncio.create_task(resolver.resolve("www.wattpad.com"))
        await asyncio.sleep(0.02)
        t2 = asyncio.create_task(resolver.resolve("www.wattpad.com"))
        await asyncio.sleep(0.02)
        t1.cancel()
        try:
            await t1
            assert False, "Expected CancelledError"
        except asyncio.CancelledError:
            pass

        res2 = await asyncio.wait_for(t2, timeout=1.0)
        assert res2 == "52.84.150.46"
        assert resolver._consecutive_failures == 0

        # Case 2: Follower (Caller B) is cancelled mid-flight; Caller A must not hit InvalidStateError.
        resolver.flush()
        ta = asyncio.create_task(resolver.resolve("discord.com"))
        await asyncio.sleep(0.02)
        tb = asyncio.create_task(resolver.resolve("discord.com"))
        await asyncio.sleep(0.02)
        tb.cancel()
        try:
            await tb
        except asyncio.CancelledError:
            pass

        resa = await asyncio.wait_for(ta, timeout=1.0)
        assert resa == "52.84.150.46"
        assert resolver._consecutive_failures == 0
        resolver.close()

    asyncio.run(_run())


def test_race_client_hello_returns_immediately_when_loser_hangs_on_close():
    """Winning tunnel socket must never wait for a DPI-blackholed losing socket's wait_closed()."""

    async def _run():
        blackhole_hold = asyncio.Event()

        async def _blackhole_srv(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                await reader.read(1024)
                await blackhole_hold.wait()
            finally:
                writer.close()

        async def _good_tunnel_srv(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            await reader.read(1024)
            # Valid TLS Handshake ServerHello prefix (0x16 0x03 0x03 ...)
            writer.write(b"\x16\x03\x03\x00\x05HELLO")
            await writer.drain()
            await asyncio.sleep(0.2)
            writer.close()

        bh = await asyncio.start_server(_blackhole_srv, "127.0.0.1", 0)
        good = await asyncio.start_server(_good_tunnel_srv, "127.0.0.1", 0)
        bh_port = bh.sockets[0].getsockname()[1]
        good_port = good.sockets[0].getsockname()[1]

        try:
            r0, w0 = await asyncio.open_connection("127.0.0.1", bh_port)

            # Simulate a socket whose wait_closed() hangs 10s if ever awaited!
            async def _hanging_wait_closed():
                await asyncio.sleep(10.0)

            w0.wait_closed = _hanging_wait_closed  # type: ignore[method-assign]

            async def _tunnel_connect():
                return await asyncio.open_connection("127.0.0.1", good_port)

            t0 = time.monotonic()
            (win_r, win_w, first_resp), strat = await asyncio.wait_for(
                race_client_hello(
                    r0,
                    w0,
                    _build_dummy_client_hello(),
                    "www.wattpad.com",
                    "127.0.0.1",
                    bh_port,
                    delay=0.0,
                    race_delay=0.05,
                    prefer_tls_records=False,
                    race_both=True,
                    force_fragment=True,
                    hello_timeout=3.0,
                    tunnel_connect=_tunnel_connect,
                    tunnel_delay=0.06,
                ),
                timeout=1.0,
            )
            elapsed = time.monotonic() - t0
            assert strat == "tunnel"
            assert first_resp.startswith(b"\x16\x03\x03")
            assert elapsed < 0.6, f"race_client_hello hung for {elapsed:.2f}s"
            win_w.close()
        finally:
            blackhole_hold.set()
            bh.close()
            good.close()
            await bh.wait_closed()
            await good.wait_closed()

    asyncio.run(_run())


def test_via_tunnel_transparently_retries_once_on_idle_stream_eof():
    """If the 1st SOCKS5 stream closes with EOF on ClientHello, ProxyServer retries cleanly once."""

    async def _run():
        attempts = {"n": 0}

        async def _flaky_socks(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                greet = await reader.readexactly(3)
                assert greet == b"\x05\x01\x00"
                writer.write(b"\x05\x00")
                await writer.drain()
                hdr = await reader.readexactly(4)
                assert hdr[:3] == b"\x05\x01\x00"
                if hdr[3] == 3:
                    ln = (await reader.readexactly(1))[0]
                    await reader.readexactly(ln + 2)
                else:
                    await reader.readexactly(6)
                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01" + struct.pack("!H", 443))
                await writer.drain()

                attempts["n"] += 1
                await reader.read(1024)
                if attempts["n"] == 1:
                    # Simulate stale idle HTTP/2 stream EOF on 1st try
                    writer.close()
                    return
                writer.write(b"\x16\x03\x03\x00\x04OKAY")
                await writer.drain()
                await asyncio.sleep(0.05)
            finally:
                writer.close()

        socks_srv = await asyncio.start_server(_flaky_socks, "127.0.0.1", 0)
        socks_port = socks_srv.sockets[0].getsockname()[1]

        proxy = ProxyServer(
            port=0,
            mode=NetworkMode.STRICT,
            doh_enabled=False,
            prewarm_doh=False,
            tunnel_port=socks_port,
            bypass_loopback=False,
        )
        await proxy.start()
        serve_task = asyncio.create_task(proxy.serve())
        try:
            cr, cw = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
            cw.write(b"CONNECT www.wattpad.com:443 HTTP/1.1\r\nHost: www.wattpad.com:443\r\n\r\n")
            await cw.drain()
            est = await asyncio.wait_for(cr.readuntil(b"\r\n\r\n"), timeout=2.0)
            assert b"200 Connection Established" in est

            cw.write(_build_dummy_client_hello("www.wattpad.com"))
            await cw.drain()
            resp = await asyncio.wait_for(cr.readexactly(9), timeout=2.0)
            assert resp == b"\x16\x03\x03\x00\x04OKAY"
            assert attempts["n"] == 2
            assert proxy.telemetry.strategy_counts["failed"] == 0
            cw.close()
        finally:
            await proxy.stop()
            await asyncio.wait_for(serve_task, timeout=1.5)
            socks_srv.close()
            await socks_srv.wait_closed()

    asyncio.run(_run())


def test_parallel_tunnel_timeouts_do_not_kill_healthy_managed_tunnel():
    """2-4 parallel Chrome timeouts must NOT restart a healthy usque process, but dead process must restart."""

    async def _run():
        restarts = []

        class FakeManagedTunnel:
            port = 11894
            host = "127.0.0.1"
            available = True

            def is_ready(self):
                return self.available

            def start(self):
                self.available = True
                return True

            def stop(self):
                restarts.append(True)

        mt = FakeManagedTunnel()
        proxy = ProxyServer(port=0, doh_enabled=False, prewarm_doh=False, managed_tunnel=mt, tunnel_port=11894)

        # 4 simultaneous Chrome socket failures while usque is alive -> must NOT restart!
        for _ in range(4):
            proxy._on_tunnel_failure()
        await asyncio.sleep(0.05)
        assert restarts == []

        # If usque process actually died -> must restart immediately!
        mt.available = False
        proxy._on_tunnel_failure()
        await asyncio.sleep(0.1)
        assert restarts == [True]
        await proxy.stop()

    asyncio.run(_run())


def test_abrupt_client_disconnect_storm_cleans_up_zero_leaks():
    """20 concurrent clients disconnecting mid-race must leave 0 active connections and stop fast."""

    async def _run():
        hold = asyncio.Event()

        async def _slow_upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                await hold.wait()
            finally:
                writer.close()

        up = await asyncio.start_server(_slow_upstream, "127.0.0.1", 0)
        up_port = up.sockets[0].getsockname()[1]

        proxy = ProxyServer(
            port=0,
            mode=NetworkMode.AUTO,
            doh_enabled=False,
            prewarm_doh=False,
            bypass_loopback=False,
        )
        await proxy.start()
        serve_task = asyncio.create_task(proxy.serve())
        try:
            async def _abrupt_client():
                r, w = await asyncio.open_connection("127.0.0.1", proxy.bound_port)
                w.write(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\nHost: 127.0.0.1:{up_port}\r\n\r\n".encode())
                await w.drain()
                await asyncio.wait_for(r.readuntil(b"\r\n\r\n"), timeout=2.0)
                w.write(_build_dummy_client_hello("example.org"))
                await w.drain()
                await asyncio.sleep(0.03)
                if w.transport is not None:
                    w.transport.abort()
                w.close()

            await asyncio.gather(*(_abrupt_client() for _ in range(20)))
            for _ in range(30):
                if proxy.telemetry.active_connections == 0:
                    break
                await asyncio.sleep(0.05)
            assert proxy.telemetry.active_connections == 0
        finally:
            hold.set()
            await asyncio.wait_for(proxy.stop(), timeout=1.5)
            await asyncio.wait_for(serve_task, timeout=1.5)
            up.close()
            await up.wait_closed()

    asyncio.run(_run())


def test_tunnel_is_strictly_last_resort_never_steals_slow_fragmentation():
    """Even if US-origin TCP fragmentation takes 450ms, tunnel must NEVER launch or steal the race."""

    async def _run():
        tunnel_calls = {"n": 0}

        async def _us_origin_srv(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                first = await reader.read(8)
                # Unfragmented ClientHello (>2 bytes at once) is silently dropped by ISP DPI;
                # fragmented ClientHello (1-2 byte initial chunk) succeeds after 450ms US RTT.
                if len(first) <= 2:
                    await asyncio.sleep(0.05)
                    _ = await reader.read(2048)
                    await asyncio.sleep(0.40)
                    writer.write(b"\x16\x03\x03\x00\x04OKAY")
                    await writer.drain()
                else:
                    await asyncio.sleep(2.0)
            finally:
                writer.close()

        async def _should_not_be_called_tunnel():
            tunnel_calls["n"] += 1
            raise AssertionError("Tunnel should never be called when fragmentation succeeds")

        srv = await asyncio.start_server(_us_origin_srv, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            r0, w0 = await asyncio.open_connection("127.0.0.1", port)
            (win_r, win_w, resp), strat = await race_client_hello(
                r0,
                w0,
                _build_dummy_client_hello("friends.roblox.com"),
                "friends.roblox.com",
                "127.0.0.1",
                port,
                delay=0.0,
                race_delay=0.05,
                prefer_tls_records=False,
                race_both=False,
                force_fragment=True,
                hello_timeout=2.0,
                tunnel_connect=_should_not_be_called_tunnel,
                tunnel_delay=None,
            )
            assert strat == "tcp_frag"
            assert resp == b"\x16\x03\x03\x00\x04OKAY"
            assert tunnel_calls["n"] == 0
            win_w.close()
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(_run())


def test_home_isp_frag_roots_prevent_root_or_cdn_tunnel_poisoning():
    """Once fragmentation works on a home ISP, a flaky host must never poison roblox.com or rbxcdn.com."""
    proxy = ProxyServer(
        port=0,
        mode=NetworkMode.AUTO,
        doh_enabled=False,
        prewarm_doh=False,
        tunnel_port=11894,
        force_fragment_domains=("roblox.com", "discord.com", "wattpad.com"),
    )
    proxy._record_frag_host("www.roblox.com", "tcp_frag")
    assert "roblox.com" in proxy._frag_roots
    assert proxy._root_frag_strategy["roblox.com"] == "tcp_frag"

    # A single transient fallback on one subdomain must NOT cache tunnel or poison siblings/CDN
    proxy._record_tunnel_host("flaky.roblox.com")
    assert "roblox.com" not in proxy._tunnel_roots
    assert "rbxcdn.com" not in proxy._tunnel_roots
    assert proxy.strategies.get("flaky.roblox.com") is None
    assert proxy._should_use_tunnel_directly("friends.roblox.com", None) is False
    assert proxy._should_use_tunnel_directly("tr.rbxcdn.com", None) is False
    assert proxy._should_use_tunnel_directly("discord.com", None) is False


def test_eduroam_auto_probe_enables_zero_wait_tunnel_and_roam_home_recovers(monkeypatch):
    """On Eduroam (where both probe hosts fail fragmentation), blocked sites route straight to MASQUE in 0ms;
    when roaming back to Home Wi-Fi (where fragmentation works), 1 Gbps TCP fragmentation resumes immediately."""

    async def _run():
        proxy = ProxyServer(
            port=0,
            mode=NetworkMode.AUTO,
            doh_enabled=True,
            prewarm_doh=True,
            tunnel_port=11894,
            force_fragment_domains=("discord.com", "roblox.com", "wattpad.com", "pastebin.com"),
        )

        async def _fake_resolve(host: str):
            return "162.159.138.232"

        monkeypatch.setattr(proxy.resolver, "resolve", _fake_resolve)

        # 1. Simulate Eduroam firewall: fragmentation completely blocked on probe hosts
        async def _eduroam_blocked_probe(host: str):
            return None

        monkeypatch.setattr(proxy, "_probe_single_frag_host", _eduroam_blocked_probe)
        await proxy._probe_network_profile()

        assert len(proxy._frag_roots) == 0
        assert {"discord.com", "roblox.com"}.issubset(proxy._tunnel_roots)
        # User opens Wattpad/Roblox/Discord on Eduroam -> 0ms instant tunnel!
        assert proxy._should_use_tunnel_directly("www.wattpad.com", None) is True
        assert proxy._should_use_tunnel_directly("friends.roblox.com", None) is True
        assert proxy._should_use_tunnel_directly("pastebin.com", None) is True
        # Unblocked high-bandwidth services (YouTube/Google) still stay direct!
        assert proxy._should_use_tunnel_directly("www.youtube.com", None) is False

        # 2. Simulate roaming back to Home 1 Gbps Wi-Fi: flush + re-probe succeeds via tcp_frag
        proxy.flush_caches(clear_persisted=False)

        async def _home_working_probe(host: str):
            return "tcp_frag"

        monkeypatch.setattr(proxy, "_probe_single_frag_host", _home_working_probe)
        await proxy._probe_network_profile()

        assert {"discord.com", "roblox.com"}.issubset(proxy._frag_roots)
        assert len(proxy._tunnel_roots) == 0
        assert proxy._should_use_tunnel_directly("www.wattpad.com", None) is False
        assert proxy._should_use_tunnel_directly("friends.roblox.com", None) is False
        assert proxy._should_use_tunnel_directly("discord.com", None) is False
        proxy.resolver.close()

    asyncio.run(_run())
