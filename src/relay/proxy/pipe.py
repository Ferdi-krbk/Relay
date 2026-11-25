"""Zero-copy transport bridging and high-throughput OS socket tuning."""
from __future__ import annotations

import asyncio
import contextlib
import socket

from relay.core.telemetry import ProxyTelemetry

SOCKET_BUFFER_BYTES = 4 * 1024 * 1024   # 4 MB kernel send/recv buffers
STREAM_LIMIT_BYTES = 2 * 1024 * 1024    # 2 MB StreamReader limit
WRITE_LOW_WATER = 1 * 1024 * 1024       # 1 MB low-water flow control


def tune_socket(writer: asyncio.StreamWriter) -> None:
    """Configure low-latency and 1 Gbps+ throughput socket options."""
    transport = writer.transport
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.set_write_buffer_limits(high=SOCKET_BUFFER_BYTES, low=WRITE_LOW_WATER)
    sock = writer.get_extra_info("socket")
    if sock is not None:
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, SOCKET_BUFFER_BYTES)
        with contextlib.suppress(OSError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, SOCKET_BUFFER_BYTES)


async def open_upstream(
    target_host: str,
    port: int,
    timeout: float = 8.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(target_host, port, limit=STREAM_LIMIT_BYTES),
        timeout=timeout,
    )
    tune_socket(writer)
    return reader, writer


class PipeProtocol(asyncio.Protocol):
    """Zero-copy transport-to-transport forwarder bypassing StreamReader buffer copies."""

    __slots__ = ("peer", "telemetry", "is_upload", "done", "orig_protocol")

    def __init__(
        self,
        peer: asyncio.Transport,
        telemetry: ProxyTelemetry,
        is_upload: bool,
        done: asyncio.Future,
        orig_protocol: asyncio.BaseProtocol | None = None,
    ):
        self.peer = peer
        self.telemetry = telemetry
        self.is_upload = is_upload
        self.done = done
        self.orig_protocol = orig_protocol

    def data_received(self, data: bytes) -> None:
        self.telemetry.add_bytes(len(data), is_upload=self.is_upload)
        if not self.peer.is_closing():
            self.peer.write(data)

    def pause_writing(self) -> None:
        if not self.peer.is_closing():
            self.peer.pause_reading()

    def resume_writing(self) -> None:
        if not self.peer.is_closing():
            self.peer.resume_reading()

    def eof_received(self) -> bool:
        if not self.peer.is_closing() and self.peer.can_write_eof():
            self.peer.write_eof()
        return False

    def connection_lost(self, exc) -> None:
        orig = self.orig_protocol
        self.orig_protocol = None
        if orig is not None:
            with contextlib.suppress(Exception):
                orig.connection_lost(exc)
        if not self.done.done():
            self.done.set_result(None)


async def bridge_streams(
    client_reader: asyncio.StreamReader,
    client_writer: asyncio.StreamWriter,
    upstream_reader: asyncio.StreamReader,
    upstream_writer: asyncio.StreamWriter,
    telemetry: ProxyTelemetry,
) -> None:
    """Attach zero-copy PipeProtocol to both transports and await tunnel completion."""
    c_tr = client_writer.transport
    u_tr = upstream_writer.transport
    if c_tr is None or u_tr is None:
        return

    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    c_orig = c_tr.get_protocol()
    u_orig = u_tr.get_protocol()
    c_tr.set_protocol(PipeProtocol(u_tr, telemetry, True, done, orig_protocol=c_orig))
    u_tr.set_protocol(PipeProtocol(c_tr, telemetry, False, done, orig_protocol=u_orig))

    # Drain any bytes already buffered inside StreamReaders during handshake.
    if client_reader._buffer:
        buf = bytes(client_reader._buffer)
        client_reader._buffer.clear()
        telemetry.add_bytes(len(buf), is_upload=True)
        if not u_tr.is_closing():
            u_tr.write(buf)
    if upstream_reader._buffer:
        buf = bytes(upstream_reader._buffer)
        upstream_reader._buffer.clear()
        telemetry.add_bytes(len(buf), is_upload=False)
        if not c_tr.is_closing():
            c_tr.write(buf)

    if client_reader.at_eof() or upstream_reader.at_eof():
        return
    await done
