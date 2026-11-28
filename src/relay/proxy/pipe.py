"""Zero-copy transport bridging and high-throughput OS socket tuning."""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
from ctypes import wintypes
import ipaddress
import os
import socket
import struct
import time

from relay.core.telemetry import ProxyTelemetry

SOCKET_BUFFER_BYTES = 1024 * 1024       # 1 MB user-space backpressure high-water (zero upload bufferbloat + full 1 Gbps+ burst headroom)
STREAM_LIMIT_BYTES = 2 * 1024 * 1024    # 2 MB StreamReader limit
WRITE_LOW_WATER = 256 * 1024            # 256 KB low-water flow control
_WIN_IP_UNICAST_IF = 31                 # Winsock IP_UNICAST_IF option (network byte order DWORD)

_if_cache_idx: int = 0
_if_cache_until: float = 0.0


if os.name == "nt":
    class _MIB_IPFORWARDROW(ctypes.Structure):
        _fields_ = [
            ("dwForwardDest", wintypes.DWORD),
            ("dwForwardMask", wintypes.DWORD),
            ("dwForwardPolicy", wintypes.DWORD),
            ("dwForwardNextHop", wintypes.DWORD),
            ("dwForwardIfIndex", wintypes.DWORD),
            ("dwForwardType", wintypes.DWORD),
            ("dwForwardProto", wintypes.DWORD),
            ("dwForwardAge", wintypes.DWORD),
            ("dwForwardNextHopAS", wintypes.DWORD),
            ("dwForwardMetric1", wintypes.DWORD),
            ("dwForwardMetric2", wintypes.DWORD),
            ("dwForwardMetric3", wintypes.DWORD),
            ("dwForwardMetric4", wintypes.DWORD),
            ("dwForwardMetric5", wintypes.DWORD),
        ]


def _query_physical_default_if_index() -> int:
    """Return physical IPv4 gateway IfIndex when RelayGameTun (172.19.0.0/30) captures default routes.

    When Game Mode (`sing-box` Wintun `auto_route`) is active, unbound outbound sockets from
    Relay would otherwise traverse `RelayGameTun`'s user-space gVisor TCP stack, reassembling
    DPI-bypass TCP fragments back into a single blocked ClientHello and capping 1 Gbps speed.
    """
    if os.name != "nt":
        return 0
    global _if_cache_idx, _if_cache_until
    now = time.monotonic()
    if now < _if_cache_until:
        return _if_cache_idx

    idx = 0
    try:
        # Zero-packet routing lookup to check whether default public routes point at RelayGameTun (172.19.*)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("198.41.0.4", 80))
            routed_ip = str(probe.getsockname()[0])
        if routed_ip.startswith("172.19."):
            iphlpapi = ctypes.windll.iphlpapi
            size = wintypes.ULONG(0)
            iphlpapi.GetIpForwardTable(None, ctypes.byref(size), False)
            if size.value > 4:
                buf = ctypes.create_string_buffer(size.value)
                if iphlpapi.GetIpForwardTable(buf, ctypes.byref(size), False) == 0:
                    count = struct.unpack_from("<I", buf, 0)[0]
                    row_size = ctypes.sizeof(_MIB_IPFORWARDROW)
                    best_metric = 1 << 30
                    offset = 4
                    for _ in range(count):
                        if offset + row_size > size.value:
                            break
                        row = _MIB_IPFORWARDROW.from_buffer_copy(buf, offset)
                        offset += row_size
                        # Physical router route is exact 0.0.0.0/0 (Wintun auto_route uses /1 split masks)
                        if row.dwForwardDest == 0 and row.dwForwardMask == 0 and row.dwForwardNextHop != 0:
                            nh = socket.inet_ntoa(struct.pack("<I", row.dwForwardNextHop))
                            if not nh.startswith("172.19.") and int(row.dwForwardMetric1) < best_metric:
                                best_metric = int(row.dwForwardMetric1)
                                idx = int(row.dwForwardIfIndex)
    except OSError:
        idx = 0

    _if_cache_idx = idx
    _if_cache_until = now + 2.0
    return idx


def _is_local_or_private_host(host: str) -> bool:
    clean = host.strip("[]").lower()
    if clean in ("localhost", "::1", "0.0.0.0") or clean.endswith(".local"):
        return True
    try:
        ip_obj = ipaddress.ip_address(clean)
        return ip_obj.is_loopback or ip_obj.is_private or ip_obj.is_link_local or ip_obj.is_unspecified
    except ValueError:
        return False


def abort_writer(writer: asyncio.StreamWriter | None) -> None:
    """Immediately tear down a failed/cancelled StreamWriter without waiting for TCP FIN drain."""
    if writer is None:
        return
    tr = writer.transport
    if tr is not None:
        with contextlib.suppress(Exception):
            tr.abort()
    with contextlib.suppress(Exception):
        writer.close()


def tune_socket(writer: asyncio.StreamWriter) -> None:
    """Configure low-latency and multi-gigabit throughput socket options.

    Intentionally preserves Windows Receive Window Auto-Tuning (never sets fixed SO_RCVBUF/SO_SNDBUF,
    which disables dynamic RFC 7323 window scaling up to 16 MB on Windows 10/11).
    """
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


async def _connect_bound_physical(
    target_host: str,
    port: int,
    if_index: int,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Connect an IPv4 socket bound via IP_UNICAST_IF to the physical NIC, bypassing Wintun."""
    loop = asyncio.get_running_loop()
    try:
        ip_obj = ipaddress.ip_address(target_host.strip("[]"))
        if ip_obj.version == 4 and not (ip_obj.is_loopback or ip_obj.is_private or ip_obj.is_link_local):
            infos = [(socket.AF_INET, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (str(ip_obj), port))]
        else:
            return await asyncio.open_connection(target_host, port, limit=STREAM_LIMIT_BYTES)
    except ValueError:
        infos = await loop.getaddrinfo(target_host, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
        if not infos:
            return await asyncio.open_connection(target_host, port, limit=STREAM_LIMIT_BYTES)

    last_err: OSError | None = None
    for af, socktype, proto, _, sockaddr in infos:
        addr_ip = str(sockaddr[0])
        if af != socket.AF_INET or _is_local_or_private_host(addr_ip):
            return await asyncio.open_connection(target_host, port, limit=STREAM_LIMIT_BYTES)
        sock = socket.socket(af, socktype, proto)
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.setsockopt(socket.IPPROTO_IP, _WIN_IP_UNICAST_IF, struct.pack("!I", if_index))
            await loop.sock_connect(sock, sockaddr)
            return await asyncio.open_connection(sock=sock, limit=STREAM_LIMIT_BYTES)
        except OSError as exc:
            last_err = exc
            with contextlib.suppress(OSError):
                sock.close()
        except BaseException:
            with contextlib.suppress(OSError):
                sock.close()
            raise
    raise last_err or ConnectionError(f"Unable to connect to {target_host}:{port}")


async def open_upstream(
    target_host: str,
    port: int,
    timeout: float = 8.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    if_idx = 0 if _is_local_or_private_host(target_host) else _query_physical_default_if_index()
    coro = (
        _connect_bound_physical(target_host, port, if_idx)
        if if_idx > 0
        else asyncio.open_connection(target_host, port, limit=STREAM_LIMIT_BYTES)
    )
    conn_task = asyncio.create_task(coro)
    try:
        reader, writer = await asyncio.wait_for(asyncio.shield(conn_task), timeout=timeout)
    except BaseException:
        if not conn_task.done():
            conn_task.cancel()
            conn_task.add_done_callback(
                lambda t: abort_writer(t.result()[1]) if not t.cancelled() and t.exception() is None else None
            )
        elif conn_task.exception() is None:
            abort_writer(conn_task.result()[1])
        raise
    tune_socket(writer)
    return reader, writer


class PipeProtocol(asyncio.Protocol):
    """Zero-copy transport-to-transport forwarder bypassing StreamReader buffer copies."""

    __slots__ = ("peer", "telemetry", "is_upload", "done", "orig_protocol", "byte_counter", "on_activity")

    def __init__(
        self,
        peer: asyncio.Transport,
        telemetry: ProxyTelemetry,
        is_upload: bool,
        done: asyncio.Future,
        orig_protocol: asyncio.BaseProtocol | None = None,
        byte_counter: list[int] | None = None,
        on_activity=None,
    ):
        self.peer = peer
        self.telemetry = telemetry
        self.is_upload = is_upload
        self.done = done
        self.orig_protocol = orig_protocol
        self.byte_counter = byte_counter
        self.on_activity = on_activity

    def data_received(self, data: bytes) -> None:
        n = len(data)
        self.telemetry.add_bytes(n, is_upload=self.is_upload)
        if self.byte_counter is not None:
            self.byte_counter[0] += n
        if self.on_activity is not None:
            self.on_activity()
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
    *,
    stall_timeout: float | None = None,
) -> bool:
    """Attach zero-copy PipeProtocol to both transports and await tunnel completion.

    Returns True if the upstream silently blackholed right after TLS ServerHello
    (client sent follow-up data, upstream sent 0 bytes within stall_timeout).
    """
    c_tr = client_writer.transport
    u_tr = upstream_writer.transport
    if c_tr is None or u_tr is None:
        return False

    loop = asyncio.get_running_loop()
    done: asyncio.Future = loop.create_future()

    up_bytes = [0]
    down_bytes = [0]
    stalled = False
    stall_timer: asyncio.TimerHandle | None = None

    def _cancel_stall_timer() -> None:
        nonlocal stall_timer
        if stall_timer is not None:
            stall_timer.cancel()
            stall_timer = None

    def _on_stall() -> None:
        nonlocal stalled
        if down_bytes[0] == 0 and up_bytes[0] > 0 and not done.done():
            stalled = True
            done.set_result(None)

    def _on_client_data() -> None:
        nonlocal stall_timer
        if stall_timeout and down_bytes[0] == 0 and stall_timer is None:
            stall_timer = loop.call_later(stall_timeout, _on_stall)

    def _on_upstream_data() -> None:
        _cancel_stall_timer()

    c_orig = c_tr.get_protocol()
    u_orig = u_tr.get_protocol()
    c_tr.set_protocol(
        PipeProtocol(
            u_tr,
            telemetry,
            True,
            done,
            orig_protocol=c_orig,
            byte_counter=up_bytes,
            on_activity=_on_client_data if stall_timeout else None,
        )
    )
    u_tr.set_protocol(
        PipeProtocol(
            c_tr,
            telemetry,
            False,
            done,
            orig_protocol=u_orig,
            byte_counter=down_bytes,
            on_activity=_on_upstream_data if stall_timeout else None,
        )
    )

    # Drain any bytes already buffered inside StreamReaders during handshake.
    if client_reader._buffer:
        buf = bytes(client_reader._buffer)
        client_reader._buffer.clear()
        up_bytes[0] += len(buf)
        telemetry.add_bytes(len(buf), is_upload=True)
        if not u_tr.is_closing():
            u_tr.write(buf)
        _on_client_data()
    if upstream_reader._buffer:
        buf = bytes(upstream_reader._buffer)
        upstream_reader._buffer.clear()
        down_bytes[0] += len(buf)
        telemetry.add_bytes(len(buf), is_upload=False)
        if not c_tr.is_closing():
            c_tr.write(buf)
        _on_upstream_data()

    if client_reader.at_eof() or upstream_reader.at_eof():
        _cancel_stall_timer()
        return False
    try:
        await done
    except BaseException:
        with contextlib.suppress(Exception):
            u_tr.abort()
        with contextlib.suppress(Exception):
            c_tr.abort()
        raise
    finally:
        _cancel_stall_timer()
        if stalled:
            with contextlib.suppress(Exception):
                u_tr.abort()
    return stalled
