"""Unit tests for TLS/TCP fragmentation planners and StrategyCache."""
import asyncio

from relay.proxy.dpi import (
    StrategyCache,
    plan_tcp_fragments,
    plan_tls_record_fragments,
    race_client_hello,
)
from relay.proxy.pipe import open_upstream


def _fake_client_hello(hostname: str) -> bytes:
    body = b"\x01\x00\x00\x40" + b"\xaa" * 32 + hostname.encode("ascii") + b"\xbb" * 24
    return b"\x16\x03\x01" + len(body).to_bytes(2, "big") + body


def test_tcp_fragment_planner_splits_across_sni_losslessly():
    hello = _fake_client_hello("discord.com")
    chunks, sni_idx = plan_tcp_fragments(hello, "discord.com")
    assert b"".join(chunks) == hello
    assert sni_idx >= 0
    # SNI must not appear intact in any single TCP chunk.
    assert all(b"discord.com" not in chunk for chunk in chunks)


def test_tls_record_planner_splits_record_envelope_across_sni():
    hello = _fake_client_hello("www.roblox.com")
    frames, sni_idx = plan_tls_record_fragments(hello, "www.roblox.com")
    assert len(frames) >= 3
    assert sni_idx >= 0
    reassembled_payload = bytearray()
    for frame in frames:
        assert frame[:3] == b"\x16\x03\x01"
        length = int.from_bytes(frame[3:5], "big")
        assert len(frame[5:]) == length
        assert b"www.roblox.com" not in frame[5:]
        reassembled_payload.extend(frame[5:])
    assert bytes(reassembled_payload) == hello[5:]


def test_strategy_cache_ttl_and_invalidation():
    cache = StrategyCache(ttl_seconds=60.0)
    assert cache.get("discord.com") is None
    cache.set("discord.com", "tcp_frag")
    assert cache.get("discord.com") == "tcp_frag"
    cache.invalidate("discord.com")
    assert cache.get("discord.com") is None


def test_force_fragment_falls_back_to_direct_when_fragmented_hellos_fail():
    async def _run():
        hello = _fake_client_hello("www.roblox.com")

        async def _upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter):
            try:
                first = await asyncio.wait_for(reader.read(4096), timeout=2.0)
                # Only an intact single-write ClientHello succeeds; fragmented chunks are rejected.
                if first == hello:
                    writer.write(b"\x16\x03\x03\x00\x04\x02\x00\x00\x00")
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        srv = await asyncio.start_server(_upstream, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        try:
            reader, writer = await open_upstream("127.0.0.1", port)
            (_r, w, resp), used = await race_client_hello(
                reader,
                writer,
                hello,
                "www.roblox.com",
                "127.0.0.1",
                port,
                delay=0.02,
                race_delay=0.1,
                prefer_tls_records=False,
                race_both=True,
                force_fragment=True,
            )
            assert used == "direct"
            assert resp.startswith(b"\x16\x03\x03")
            w.close()
            await w.wait_closed()
        finally:
            srv.close()
            await srv.wait_closed()

    asyncio.run(_run())
