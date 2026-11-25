"""Integration tests exercising the real local async ProxyServer end-to-end."""
import asyncio
import json

from relay.core.config import NetworkMode
from relay.proxy.server import ProxyServer


async def _raw_http(port: int, request_bytes: bytes) -> bytes:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    try:
        writer.write(request_bytes)
        await writer.drain()
        return await asyncio.wait_for(reader.read(65536), timeout=3.0)
    finally:
        writer.close()
        await writer.wait_closed()


def test_proxy_server_control_endpoints_http_forward_and_connect():
    async def _run():
        # 1. Start a tiny local upstream echo/HTTP server for deterministic offline tests.
        async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                req = await reader.read(4096)
                if req.startswith(b"GET /ping"):
                    body = b"pong-from-upstream"
                    writer.write(
                        b"HTTP/1.1 200 OK\r\nContent-Length: "
                        + str(len(body)).encode()
                        + b"\r\nConnection: close\r\n\r\n"
                        + body
                    )
                    await writer.drain()
                else:
                    # Echo raw tunnel payload back (non-TLS CONNECT test).
                    writer.write(b"ECHO:" + req)
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
            # /health
            raw = await _raw_http(p_port, b"GET /health HTTP/1.1\r\nHost: localhost\r\n\r\n")
            health = json.loads(raw.split(b"\r\n\r\n", 1)[1])
            assert health["ok"] is True
            assert health["mode"] == "auto"

            # /proxy.pac
            raw_pac = await _raw_http(p_port, b"GET /proxy.pac HTTP/1.1\r\nHost: localhost\r\n\r\n")
            assert f"PROXY 127.0.0.1:{p_port}; DIRECT".encode() in raw_pac
            assert b"isInNet" not in raw_pac

            # Plain HTTP proxy forwarding
            raw_http = await _raw_http(
                p_port,
                f"GET http://127.0.0.1:{up_port}/ping HTTP/1.1\r\nHost: 127.0.0.1:{up_port}\r\n\r\n".encode(),
            )
            assert raw_http.endswith(b"pong-from-upstream")

            # CONNECT tunnel + zero-copy PipeProtocol bridge
            reader, writer = await asyncio.open_connection("127.0.0.1", p_port)
            try:
                writer.write(f"CONNECT 127.0.0.1:{up_port} HTTP/1.1\r\nHost: 127.0.0.1:{up_port}\r\n\r\n".encode())
                await writer.drain()
                resp = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3.0)
                assert b"200 Connection Established" in resp
                writer.write(b"HELLO_TUNNEL_PAYLOAD")
                await writer.drain()
                echoed = await asyncio.wait_for(reader.read(4096), timeout=3.0)
                assert echoed == b"ECHO:HELLO_TUNNEL_PAYLOAD"
            finally:
                writer.close()
                await writer.wait_closed()

            # /stats
            raw_stats = await _raw_http(p_port, b"GET /stats HTTP/1.1\r\nHost: localhost\r\n\r\n")
            stats = json.loads(raw_stats.split(b"\r\n\r\n", 1)[1])
            assert stats["total_connections"] >= 2
            assert stats["bytes_down"] > 0

            # POST /shutdown cleanly exits serve_task
            raw_stop = await _raw_http(p_port, b"POST /shutdown HTTP/1.1\r\nHost: localhost\r\n\r\n")
            assert json.loads(raw_stop.split(b"\r\n\r\n", 1)[1])["stopping"] is True
            await asyncio.wait_for(serve_task, timeout=3.0)
        finally:
            up_srv.close()
            await up_srv.wait_closed()
            if not serve_task.done():
                await proxy.stop()
                await serve_task

    asyncio.run(_run())


def test_proxy_server_socks5_tunnel_fallback():
    async def _run():
        # Mock upstream that drops TLS ClientHello (simulating stateful DPI RST)
        async def _blocked_upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            await reader.read(64)
            writer.close()
            await writer.wait_closed()

        # Mock local SOCKS5 server that accepts CONNECT and returns a valid TLS ServerHello byte (0x16)
        socks_hits = 0

        async def _mock_socks5(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            nonlocal socks_hits
            try:
                greeting = await reader.readexactly(3)
                assert greeting == b"\x05\x01\x00"
                writer.write(b"\x05\x00")
                await writer.drain()

                head = await reader.readexactly(4)
                assert head[0] == 5 and head[1] == 1
                atyp = head[3]
                if atyp == 1:
                    await reader.readexactly(6)
                elif atyp == 3:
                    ln = (await reader.readexactly(1))[0]
                    await reader.readexactly(ln + 2)
                elif atyp == 4:
                    await reader.readexactly(18)

                writer.write(b"\x05\x00\x00\x01\x7f\x00\x00\x01\x00\x00")
                await writer.drain()

                hello = await reader.read(4096)
                assert hello.startswith(b"\x16\x03")
                socks_hits += 1
                writer.write(b"\x16\x03\x03\x00\x04OKAY")
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        blocked_srv = await asyncio.start_server(_blocked_upstream, "127.0.0.1", 0)
        blocked_port = blocked_srv.sockets[0].getsockname()[1]

        socks_srv = await asyncio.start_server(_mock_socks5, "127.0.0.1", 0)
        socks_port = socks_srv.sockets[0].getsockname()[1]

        proxy = ProxyServer(
            port=0,
            mode=NetworkMode.AUTO,
            doh_enabled=False,
            prewarm_doh=False,
            tunnel_port=socks_port,
        )
        await proxy.start()
        p_port = proxy.bound_port
        serve_task = asyncio.create_task(proxy.serve())

        fake_client_hello = b"\x16\x03\x01\x00\x10" + b"0123456789abcdef"

        try:
            for _ in range(2):
                reader, writer = await asyncio.open_connection("127.0.0.1", p_port)
                try:
                    writer.write(
                        f"CONNECT 127.0.0.1:{blocked_port} HTTP/1.1\r\nHost: 127.0.0.1:{blocked_port}\r\n\r\n".encode()
                    )
                    await writer.drain()
                    resp = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=3.0)
                    assert b"200 Connection Established" in resp
                    writer.write(fake_client_hello)
                    await writer.drain()
                    srv_hello = await asyncio.wait_for(reader.read(4096), timeout=3.0)
                    assert srv_hello == b"\x16\x03\x03\x00\x04OKAY"
                finally:
                    writer.close()
                    await writer.wait_closed()

            assert socks_hits == 2
            assert proxy.strategies.get("127.0.0.1") == "tunnel"
            assert proxy.telemetry.strategy_counts["tunnel"] == 2
        finally:
            await proxy.stop()
            await serve_task
            blocked_srv.close()
            await blocked_srv.wait_closed()
            socks_srv.close()
            await socks_srv.wait_closed()

    asyncio.run(_run())
