"""TLS ClientHello inspection, TCP/TLS fragmentation, and multi-strategy racing."""
from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path
import time
from typing import Awaitable, Callable

from relay.proxy.pipe import open_upstream

TLS_RECORD_HANDSHAKE = 0x16
MAX_CLIENT_HELLO_LEN = 18432
MIN_SNI_SPLIT_PAUSE = 0.008  # 8 ms prevents OS/NIC TSO from coalescing split SNI halves
LEARNED_PERSIST_TTL_SECONDS = 7 * 24 * 3600.0


class StrategyCache:
    """Bounded TTL cache remembering the winning handshake strategy per hostname."""

    def __init__(
        self,
        ttl_seconds: float = 900.0,
        max_entries: int = 2048,
        persist_path: Path | None = None,
    ):
        self.ttl_seconds = ttl_seconds
        self.max_entries = max_entries
        self.persist_path = persist_path
        self._entries: dict[str, tuple[str, float]] = {}
        self._persisted: dict[str, tuple[str, float]] = {}
        if self.persist_path is not None:
            self.load_persisted()

    def __len__(self) -> int:
        return len(self._entries)

    def get(self, hostname: str) -> str | None:
        entry = self._entries.get(hostname)
        if not entry:
            return None
        strategy, expires_at = entry
        if expires_at <= time.monotonic():
            self._entries.pop(hostname, None)
            return None
        return strategy

    def set(self, hostname: str, strategy: str, *, persist: bool = False) -> None:
        now = time.monotonic()
        if len(self._entries) >= self.max_entries:
            expired = [k for k, (_, exp) in self._entries.items() if exp <= now]
            for key in expired:
                self._entries.pop(key, None)
            if len(self._entries) >= self.max_entries:
                self._entries.clear()
        self._entries[hostname] = (strategy, now + self.ttl_seconds)
        if persist and strategy in ("tcp_frag", "tls_record"):
            prev = self._persisted.get(hostname)
            wall_exp = time.time() + LEARNED_PERSIST_TTL_SECONDS
            self._persisted[hostname] = (strategy, wall_exp)
            if prev is None or prev[0] != strategy:
                self.save_persisted()

    def invalidate(self, hostname: str) -> None:
        self._entries.pop(hostname, None)
        if hostname in self._persisted:
            self._persisted.pop(hostname, None)
            self.save_persisted()

    def clear(self, *, include_persisted: bool = False) -> None:
        self._entries.clear()
        if include_persisted:
            self._persisted.clear()
            if self.persist_path is not None:
                with contextlib.suppress(OSError):
                    self.persist_path.unlink(missing_ok=True)

    def load_persisted(self) -> None:
        if self.persist_path is None or not self.persist_path.is_file():
            return
        try:
            raw = json.loads(self.persist_path.read_text(encoding="utf-8-sig"))
            if not isinstance(raw, dict):
                return
            now_wall = time.time()
            now_mono = time.monotonic()
            for host, item in raw.items():
                if not isinstance(host, str) or not isinstance(item, dict):
                    continue
                strat = item.get("strategy")
                exp_wall = float(item.get("expires_at", 0.0))
                if strat in ("tcp_frag", "tls_record") and exp_wall > now_wall:
                    self._persisted[host] = (strat, exp_wall)
                    self._entries[host] = (strat, now_mono + self.ttl_seconds)
        except (OSError, ValueError, TypeError):
            pass

    def save_persisted(self) -> None:
        if self.persist_path is None:
            return
        now_wall = time.time()
        clean = {
            host: {"strategy": strat, "expires_at": exp}
            for host, (strat, exp) in self._persisted.items()
            if exp > now_wall and strat in ("tcp_frag", "tls_record")
        }
        try:
            self.persist_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.persist_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(self.persist_path)
        except OSError:
            pass


def plan_tcp_fragments(record: bytes, hostname: str) -> tuple[list[bytes], int]:
    """Pure planner returning TCP segment bytes and the index right after SNI midpoint."""
    name = hostname.encode("idna")
    name_at = record.find(name)
    sni_mid = (name_at + max(1, len(name) // 2)) if name_at >= 5 else -1

    points = [1, 2]
    if sni_mid > 2:
        points.extend([name_at, sni_mid, name_at + len(name)])
    else:
        points.extend(range(8, min(len(record), 96), 8))

    chunks: list[bytes] = []
    sni_chunk_index = -1
    start = 0
    for end in sorted(set(points + [len(record)])):
        if end <= start or end > len(record):
            continue
        chunks.append(record[start:end])
        if end == sni_mid:
            sni_chunk_index = len(chunks) - 1
        start = end
    return chunks, sni_chunk_index


def plan_tls_record_fragments(record: bytes, hostname: str) -> tuple[list[bytes], int]:
    """Pure planner splitting a single TLS ClientHello record into valid sub-records across SNI."""
    if len(record) <= 7:
        return [record], -1

    name = hostname.encode("idna")
    name_at = record.find(name)
    sni_mid = (name_at + max(1, len(name) // 2)) if name_at >= 5 else -1

    cuts = [5 + 2]
    if sni_mid > 5:
        cuts.append(sni_mid)
    else:
        cuts.append(min(len(record), 5 + 40))

    prefix = record[:3]
    frames: list[bytes] = []
    sni_frame_index = -1
    start = 5
    for end in sorted(set(cuts + [len(record)])):
        if end <= start or end > len(record):
            continue
        payload = record[start:end]
        frames.append(prefix + len(payload).to_bytes(2, "big") + payload)
        if end == sni_mid:
            sni_frame_index = len(frames) - 1
        start = end
    return frames, sni_frame_index


async def read_client_hello(reader: asyncio.StreamReader, timeout: float = 10.0) -> tuple[bytes, bool]:
    header = await asyncio.wait_for(reader.readexactly(5), timeout=timeout)
    length = int.from_bytes(header[3:5], "big")
    if header[0] != TLS_RECORD_HANDSHAKE or header[1] != 3 or length > MAX_CLIENT_HELLO_LEN:
        return header, False
    body = await asyncio.wait_for(reader.readexactly(length), timeout=timeout)
    return header + body, True


async def send_fragmented_hello(
    record: bytes,
    writer: asyncio.StreamWriter,
    hostname: str,
    delay: float,
    *,
    tls_records: bool = False,
) -> None:
    if tls_records:
        frames, sni_idx = plan_tls_record_fragments(record, hostname)
    else:
        frames, sni_idx = plan_tcp_fragments(record, hostname)

    last = len(frames) - 1
    for idx, frame in enumerate(frames):
        writer.write(frame)
        await writer.drain()
        if idx < last:
            if idx == sni_idx:
                pause = max(delay, MIN_SNI_SPLIT_PAUSE)
            elif tls_records:
                pause = max(delay, 0.002)
            else:
                pause = delay
            await asyncio.sleep(pause)


async def exchange_hello(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    hello: bytes,
    hostname: str,
    delay: float,
    strategy: str,
    timeout: float = 4.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    try:
        if strategy == "direct":
            writer.write(hello)
            await writer.drain()
        elif strategy == "tls_record":
            await send_fragmented_hello(hello, writer, hostname, delay, tls_records=True)
        else:
            await send_fragmented_hello(hello, writer, hostname, delay, tls_records=False)

        response = await asyncio.wait_for(reader.read(65536), timeout=timeout)
        if not response:
            raise ConnectionError("TLS connection closed before ServerHello")
        # A genuine TLS ServerHello starts with 0x16 (Handshake). Reject DPI-injected
        # TLS Alerts (0x15) or plain HTTP block redirects so fragmentation wins the race.
        if response[0] != TLS_RECORD_HANDSHAKE:
            raise ConnectionError(f"Rejected non-handshake TLS record 0x{response[0]:02x}")
        return reader, writer, response
    except BaseException:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        raise


async def _connect_and_exchange(
    target_host: str,
    port: int,
    hello: bytes,
    hostname: str,
    delay: float,
    strategy: str,
    timeout: float = 4.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await open_upstream(target_host, port)
    return await exchange_hello(reader, writer, hello, hostname, delay, strategy, timeout=timeout)


async def _tunnel_and_exchange(
    tunnel_connect: Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]],
    hello: bytes,
    hostname: str,
    timeout: float = 6.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes]:
    reader, writer = await tunnel_connect()
    return await exchange_hello(reader, writer, hello, hostname, 0.0, "direct", timeout=timeout)


async def race_client_hello(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
    hello: bytes,
    hostname: str,
    target_host: str,
    port: int,
    *,
    delay: float,
    race_delay: float,
    prefer_tls_records: bool,
    race_both: bool,
    force_fragment: bool = False,
    hello_timeout: float = 4.0,
    tunnel_connect: Callable[[], Awaitable[tuple[asyncio.StreamReader, asyncio.StreamWriter]]] | None = None,
    tunnel_delay: float = 0.9,
) -> tuple[tuple[asyncio.StreamReader, asyncio.StreamWriter, bytes], str]:
    """Race TLS handshake strategies; the winning upstream socket carries the session."""
    all_tasks: list[tuple[asyncio.Task, str]] = []
    winner = None
    last_error: BaseException | None = None
    tunnel_launched = False

    primary_frag = "tls_record" if prefer_tls_records else "tcp_frag"
    secondary_frag = "tcp_frag" if prefer_tls_records else "tls_record"

    try:
        if not force_fragment:
            direct_task = asyncio.create_task(
                exchange_hello(reader, writer, hello, hostname, delay, "direct", timeout=hello_timeout)
            )
            all_tasks.append((direct_task, "direct"))
            completed, _ = await asyncio.wait({direct_task}, timeout=race_delay)
            if completed:
                try:
                    winner = direct_task.result()
                    return winner, "direct"
                except (OSError, asyncio.TimeoutError) as error:
                    last_error = error
        else:
            primary_task = asyncio.create_task(
                exchange_hello(reader, writer, hello, hostname, delay, primary_frag, timeout=hello_timeout)
            )
            all_tasks.append((primary_task, primary_frag))

        if not force_fragment:
            frag1 = asyncio.create_task(
                _connect_and_exchange(target_host, port, hello, hostname, delay, primary_frag, timeout=hello_timeout)
            )
            all_tasks.append((frag1, primary_frag))
        else:
            # Even when fragmentation is preferred, keep direct as a parallel racer so
            # DoH-only blocked hosts (where DPI/CDNs reject fragmented hellos) still succeed.
            direct_fallback = asyncio.create_task(
                _connect_and_exchange(target_host, port, hello, hostname, delay, "direct", timeout=hello_timeout)
            )
            all_tasks.append((direct_fallback, "direct"))

        if race_both:
            frag2 = asyncio.create_task(
                _connect_and_exchange(
                    target_host, port, hello, hostname, max(delay, 0.005), secondary_frag, timeout=hello_timeout
                )
            )
            all_tasks.append((frag2, secondary_frag))

        task_to_strat = {t: s for t, s in all_tasks}
        pending = {t for t, _ in all_tasks if not t.done()}
        for task, strat in all_tasks:
            if task.done() and task not in pending:
                with contextlib.suppress(BaseException):
                    if task.exception() is None:
                        winner = task.result()
                        return winner, strat

        tunnel_deadline = time.monotonic() + max(0.05, tunnel_delay)
        while pending or (tunnel_connect is not None and not tunnel_launched):
            if not pending and tunnel_connect is not None and not tunnel_launched:
                t_task = asyncio.create_task(_tunnel_and_exchange(tunnel_connect, hello, hostname))
                all_tasks.append((t_task, "tunnel"))
                task_to_strat[t_task] = "tunnel"
                pending.add(t_task)
                tunnel_launched = True

            wait_timeout: float | None = None
            if tunnel_connect is not None and not tunnel_launched:
                wait_timeout = max(0.0, tunnel_deadline - time.monotonic())

            completed, pending = await asyncio.wait(
                pending,
                timeout=wait_timeout,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in completed:
                try:
                    winner = task.result()
                    return winner, task_to_strat[task]
                except (OSError, asyncio.TimeoutError) as error:
                    last_error = error

            if not completed and tunnel_connect is not None and not tunnel_launched:
                t_task = asyncio.create_task(_tunnel_and_exchange(tunnel_connect, hello, hostname))
                all_tasks.append((t_task, "tunnel"))
                task_to_strat[t_task] = "tunnel"
                pending.add(t_task)
                tunnel_launched = True

        raise last_error or ConnectionError("No TLS handshake strategy succeeded")
    finally:
        for task, _ in all_tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*(t for t, _ in all_tasks), return_exceptions=True)
        for result in results:
            if isinstance(result, tuple) and (winner is None or result[1] is not winner[1]):
                result[1].close()
                with contextlib.suppress(OSError):
                    await result[1].wait_closed()
