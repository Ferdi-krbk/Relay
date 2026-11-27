"""Tests verifying PipeProtocol task cleanup, DoH NXDOMAIN handling, HTTP close, token auth, hot-reload, and watchdog."""
import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

from relay.core.config import ConfigStore, NetworkMode, RelayConfig
from relay.proxy.doh import DoHResolver
from relay.proxy.dpi import StrategyCache
from relay.proxy.server import ProxyServer
from relay.service.controller import RelayService


async def _raw_http(port: int, request_bytes: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request_bytes)
        await writer.drain()
        return await asyncio.wait_for(reader.read(65536), timeout=3.0)
    finally:
        writer.close()
        await writer.wait_closed()


def test_pipe_protocol_leaves_zero_pending_tasks_and_enforces_http_connection_close():
    async def _run():
        captured_http_request = bytearray()

        async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                req = await reader.read(4096)
                if req.startswith(b"GET /check"):
                    captured_http_request.extend(req)
                    body = b"ok-http"
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\nConnection: close\r\n\r\n"
                        + body
                    )
                    await writer.drain()
                else:
                    writer.write(b"TUN:" + req)
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        up_srv = await asyncio.start_server(_upstream, "127.0.0.1", 0)
        up_port = up_srv.sockets[0].getsockname()[1]

        proxy = ProxyServer(port=0, mode=NetworkMode.AUTO, doh_enabled=False, prewarm_doh=False)
        await proxy.start()
        p_port = proxy.bound_port
        serve_task = asyncio.create_task(proxy.serve())

        try:
            # 1. Plain HTTP with Keep-Alive must be rewritten to Connection: close.
            resp = await _raw_http(
                p_port,
                (
                    f"GET http://127.0.0.1:{up_port}/check HTTP/1.1\r\n"
                    f"Host: 127.0.0.1:{up_port}\r\n"
                    f"Proxy-Connection: keep-alive\r\n"
                    f"Connection: keep-alive\r\n\r\n"
                ).encode(),
            )
            assert resp.endswith(b"ok-http")
            raw_forwarded = bytes(captured_http_request).lower()
            assert b"connection: close" in raw_forwarded
            assert b"connection: keep-alive" not in raw_forwarded
            assert b"proxy-connection:" not in raw_forwarded

            # 2. Run 10 CONNECT tunnels through zero-copy PipeProtocol and verify zero leaked tasks.
            for idx in range(10):
                reader, writer = await asyncio.open_connection("127.0.0.1", p_port)
                writer.write(
                    f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\nHost: 127.0.0.1:{up_port}\r\n\r\n".encode()
                )
                await writer.drain()
                head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3.0)
                assert b"200 Connection Established" in head
                payload = f"MSG-{idx}".encode()
                writer.write(payload)
                await writer.drain()
                echoed = await asyncio.wait_for(reader.read(4096), timeout=3.0)
                assert echoed == b"TUN:" + payload
                writer.close()
                await writer.wait_closed()

            # Shut down proxy and upstream server, then let loop settle.
            await proxy.stop()
            await asyncio.wait_for(serve_task, timeout=3.0)
            up_srv.close()
            await up_srv.wait_closed()
            await asyncio.sleep(0.05)

            current = asyncio.current_task()
            leftover = [
                t for t in asyncio.all_tasks() if t is not current and not t.done()
            ]
            assert leftover == [], f"Leaked pending tasks: {leftover}"
        finally:
            up_srv.close()
            await up_srv.wait_closed()
            if not serve_task.done():
                await proxy.stop()
                await serve_task

    asyncio.run(_run())


def test_doh_nxdomain_does_not_trip_circuit_breaker_and_cache_is_bounded():
    async def _run():
        resolver = DoHResolver(enabled=True, max_entries=8)
        # Simulate valid DoH NXDOMAIN / NODATA responses (reached=True, ip=None).
        resolver._query_doh = AsyncMock(return_value=(None, 60, True))

        for idx in range(5):
            ip = await resolver.resolve(f"nonexistent-{idx}.example")
            assert ip is None

        # Must NOT have tripped circuit breaker or incremented failures!
        assert resolver._consecutive_failures == 0
        assert resolver._backoff_until == 0.0
        assert resolver.fallbacks == 0

        # Second query for the same domain must hit negative cache (no extra _query_doh call).
        calls_before = resolver._query_doh.await_count
        assert await resolver.resolve("nonexistent-0.example") is None
        assert resolver._query_doh.await_count == calls_before

        # Cache upper bound must be respected.
        for idx in range(25):
            resolver._cache_set(f"host-{idx}.example", "1.2.3.4", 9999999999.0)
        assert len(resolver._cache) <= 8

    asyncio.run(_run())


def test_control_endpoints_token_auth_origin_rejection_and_hot_reload():
    async def _run():
        token = "super-secret-local-token-1234"
        proxy = ProxyServer(
            port=0,
            mode=NetworkMode.AUTO,
            doh_enabled=False,
            prewarm_doh=False,
            control_token=token,
        )
        await proxy.start()
        p_port = proxy.bound_port
        serve_task = asyncio.create_task(proxy.serve())

        try:
            # 1. Missing token -> 403 Forbidden.
            raw = await _raw_http(p_port, b"POST /shutdown HTTP/1.1\r\nHost: localhost\r\n\r\n")
            assert raw.startswith(b"HTTP/1.1 403 Forbidden")
            assert not proxy.shutdown_event.is_set()

            # 2. Valid token BUT browser Origin header -> 403 Forbidden.
            raw_origin = await _raw_http(
                p_port,
                (
                    f"POST /shutdown HTTP/1.1\r\nHost: localhost\r\n"
                    f"X-Relay-Token: {token}\r\nOrigin: https://evil.example\r\n\r\n"
                ).encode(),
            )
            assert raw_origin.startswith(b"HTTP/1.1 403 Forbidden")
            assert not proxy.shutdown_event.is_set()

            # 3. Valid token on POST /config -> live hot-reload without restarting.
            body = json.dumps(
                {
                    "mode": "compat",
                    "doh_enabled": True,
                    "bypass_domains": ["banka.com.tr"],
                    "force_fragment_domains": ["discord.com", "roblox.com"],
                }
            ).encode("utf-8")
            raw_cfg = await _raw_http(
                p_port,
                (
                    f"POST /config HTTP/1.1\r\nHost: localhost\r\n"
                    f"X-Relay-Token: {token}\r\n"
                    f"Content-Length: {len(body)}\r\n\r\n"
                ).encode()
                + body,
            )
            assert raw_cfg.startswith(b"HTTP/1.1 200 OK")
            assert proxy.mode == NetworkMode.COMPAT
            assert proxy.resolver.enabled is True
            assert "banka.com.tr" in proxy.bypass_domains

            # 4. Valid token on POST /flush clears strategy cache.
            proxy.strategies.set("example.com", "tcp_frag")
            assert len(proxy.strategies) == 1
            raw_flush = await _raw_http(
                p_port,
                f"POST /flush HTTP/1.1\r\nHost: localhost\r\nX-Relay-Token: {token}\r\n\r\n".encode(),
            )
            assert raw_flush.startswith(b"HTTP/1.1 200 OK")
            assert len(proxy.strategies) == 0

            # 5. Valid token on POST /shutdown cleanly exits.
            raw_stop = await _raw_http(
                p_port,
                f"POST /shutdown HTTP/1.1\r\nHost: localhost\r\nX-Relay-Token: {token}\r\n\r\n".encode(),
            )
            assert raw_stop.startswith(b"HTTP/1.1 200 OK")
            await asyncio.wait_for(serve_task, timeout=3.0)
        finally:
            if not serve_task.done():
                await proxy.stop()
                await serve_task

    asyncio.run(_run())


def test_strategy_cache_persists_only_dpi_strategies(tmp_path: Path):
    learned = tmp_path / "learned-strategies.json"
    cache = StrategyCache(persist_path=learned)
    cache.set("open.example", "direct", persist=True)
    cache.set("blocked.example", "tcp_frag", persist=True)
    assert learned.is_file()

    reloaded = StrategyCache(persist_path=learned)
    assert reloaded.get("blocked.example") == "tcp_frag"
    assert reloaded.get("open.example") is None


def test_service_hot_reload_and_watchdog_recovery(tmp_path: Path):
    class FakeProxyStore:
        def __init__(self):
            self.port = 11893
            self.applied = []
            self.restored = False
            self.baseline = [{"Name": "ProxyEnable", "Exists": True, "Value": 0, "Kind": 4}]

        def active_port(self):
            return self.port

        def capture_baseline(self):
            return list(self.baseline)

        def load_baseline(self):
            return list(self.baseline)

        def save_baseline(self, baseline):
            self.baseline = list(baseline)

        def clear_baseline(self):
            pass

        def apply_proxy(self, port, *, system_proxy):
            self.port = port
            self.applied.append((port, system_proxy))

        def restore_baseline(self, baseline):
            self.port = None
            self.restored = True

        def set_boot_safety_net(self, enabled):
            pass

    class FakeWorkerManager:
        def __init__(self):
            self.ready = True
            self.starts = 0
            self.hot_updates = 0
            self.fail_start = False

        def health(self, port):
            return {"ok": True} if self.ready else None

        def stats(self, port):
            return {"ok": True} if self.ready else None

        def is_ready(self, port):
            return self.ready

        def start(self, port, config, *, force_restart=False):
            if self.fail_start:
                raise RuntimeError("boom")
            self.starts += 1
            self.ready = True

        def stop(self, port):
            self.ready = False

        def update_config(self, port, config):
            self.hot_updates += 1
            return True

        def flush(self, port, *, clear_persisted=False):
            return True

    store = FakeProxyStore()
    worker = FakeWorkerManager()
    svc = RelayService(ConfigStore(tmp_path), store, worker, port=11893)

    # Hot-reload must update worker in-place without restarting it.
    svc.update_config(mode=NetworkMode.FAST)
    svc.apply_config_live()
    assert worker.hot_updates == 1
    assert worker.starts == 0

    # Watchdog: worker dies -> recover_if_needed restarts it automatically.
    worker.ready = False
    st = svc.recover_if_needed()
    assert st.connected is True
    assert worker.starts == 1

    # Watchdog: worker dies and restart fails -> baseline is safely restored!
    worker.ready = False
    worker.fail_start = True
    try:
        svc.recover_if_needed()
        assert False, "Expected RuntimeError"
    except RuntimeError:
        pass
    assert store.restored is True
    assert store.active_port() is None


def test_shutdown_with_idle_open_connect_tunnel_does_not_hang():
    """Regression: open keep-alive CONNECT tunnels using PipeProtocol must not hang shutdown."""

    async def _run():
        hold = asyncio.Event()

        async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                await hold.wait()
            finally:
                writer.close()

        up_srv = await asyncio.start_server(_upstream, "127.0.0.1", 0)
        up_port = up_srv.sockets[0].getsockname()[1]

        proxy = ProxyServer(port=0, mode=NetworkMode.AUTO, doh_enabled=False, prewarm_doh=False)
        await proxy.start()
        p_port = proxy.bound_port
        serve_task = asyncio.create_task(proxy.serve())

        reader, writer = await asyncio.open_connection("127.0.0.1", p_port)
        try:
            writer.write(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\nHost: 127.0.0.1:{up_port}\r\n\r\n".encode())
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=2.0)
            assert b"200 Connection Established" in head
            await asyncio.sleep(0.05)

            # Stop proxy while client + upstream keep-alive sockets are still open and idle.
            await asyncio.wait_for(proxy.stop(), timeout=1.5)
            await asyncio.wait_for(serve_task, timeout=1.5)
        finally:
            hold.set()
            writer.close()
            up_srv.close()
            await up_srv.wait_closed()

    asyncio.run(_run())


def test_windows_proxy_store_restores_clean_wininet_per_connection(tmp_path: Path, monkeypatch):
    import winreg
    from relay.platform import registry

    per_conn_calls = []
    monkeypatch.setattr(
        registry,
        "_apply_wininet_per_connection",
        lambda **kw: per_conn_calls.append(kw),
    )
    monkeypatch.setattr(registry, "notify_windows_network_changed", lambda: None)

    fake_reg: dict[str, tuple[object, int]] = {}

    class DummyKey:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(winreg, "OpenKey", lambda *a, **kw: DummyKey())

    def _set_val(_k, name, _res, kind, val):
        fake_reg[name] = (val, kind)

    def _del_val(_k, name):
        if name not in fake_reg:
            raise FileNotFoundError(name)
        fake_reg.pop(name)

    def _query_val(_k, name):
        if name not in fake_reg:
            raise FileNotFoundError(name)
        return fake_reg[name]

    monkeypatch.setattr(winreg, "SetValueEx", _set_val)
    monkeypatch.setattr(winreg, "DeleteValue", _del_val)
    monkeypatch.setattr(winreg, "QueryValueEx", _query_val)

    store = registry.WindowsProxyStore(tmp_path)
    store.apply_proxy(11893, system_proxy=True)
    assert per_conn_calls[-1] == {
        "flags": registry.PROXY_TYPE_DIRECT | registry.PROXY_TYPE_PROXY,
        "proxy_server": "127.0.0.1:11893",
        "proxy_bypass": registry.PROXY_OVERRIDE,
        "autoconfig_url": "",
    }

    # Even if baseline accidentally had a stale Relay PAC URL, restore_baseline must strip it and reset to DIRECT.
    dirty_baseline = [
        {"Name": "ProxyEnable", "Exists": True, "Value": 0, "Kind": winreg.REG_DWORD},
        {"Name": "AutoConfigURL", "Exists": True, "Value": "http://127.0.0.1:11893/proxy.pac", "Kind": winreg.REG_SZ},
        {"Name": "ProxyServer", "Exists": False},
        {"Name": "ProxyOverride", "Exists": False},
    ]
    store.restore_baseline(dirty_baseline)
    assert "AutoConfigURL" not in fake_reg
    assert per_conn_calls[-1] == {
        "flags": registry.PROXY_TYPE_DIRECT,
        "proxy_server": "",
        "proxy_bypass": "",
        "autoconfig_url": "",
    }
