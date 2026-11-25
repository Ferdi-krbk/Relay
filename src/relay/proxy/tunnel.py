"""Local SOCKS5 upstream client and managed Cloudflare WARP MASQUE (usque) tunnel."""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time

from relay.core.config import TUNNEL_PORT, default_data_dir
from relay.proxy.pipe import STREAM_LIMIT_BYTES, tune_socket


async def open_socks5_upstream(
    socks_host: str,
    socks_port: int,
    target_host: str,
    target_port: int,
    timeout: float = 6.0,
) -> tuple[asyncio.StreamReader, asyncio.StreamWriter]:
    """Open a TCP stream to (target_host, target_port) through a local RFC 1928 SOCKS5 proxy."""
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(socks_host, socks_port, limit=STREAM_LIMIT_BYTES),
        timeout=timeout,
    )
    try:
        tune_socket(writer)

        # 1. Greeting: VER=5, NMETHODS=1, METHOD=0x00 (No Auth)
        writer.write(b"\x05\x01\x00")
        await writer.drain()
        greeting = await asyncio.wait_for(reader.readexactly(2), timeout=timeout)
        if greeting != b"\x05\x00":
            raise ConnectionError(f"SOCKS5 handshake rejected: {greeting!r}")

        # 2. CONNECT request
        addr_payload: bytes
        try:
            ip_obj = ipaddress.ip_address(target_host)
            if isinstance(ip_obj, ipaddress.IPv4Address):
                addr_payload = b"\x01" + ip_obj.packed
            else:
                addr_payload = b"\x04" + ip_obj.packed
        except ValueError:
            host_bytes = target_host.encode("idna")
            if len(host_bytes) > 255:
                raise ValueError("Target hostname too long for SOCKS5")
            addr_payload = b"\x03" + bytes([len(host_bytes)]) + host_bytes

        req = b"\x05\x01\x00" + addr_payload + struct.pack("!H", int(target_port))
        writer.write(req)
        await writer.drain()

        # 3. CONNECT reply
        reply_head = await asyncio.wait_for(reader.readexactly(4), timeout=timeout)
        if reply_head[0] != 0x05 or reply_head[1] != 0x00:
            raise ConnectionError(f"SOCKS5 CONNECT failed (status=0x{reply_head[1]:02x})")

        atyp = reply_head[3]
        if atyp == 0x01:
            await asyncio.wait_for(reader.readexactly(4 + 2), timeout=timeout)
        elif atyp == 0x04:
            await asyncio.wait_for(reader.readexactly(16 + 2), timeout=timeout)
        elif atyp == 0x03:
            domain_len = (await asyncio.wait_for(reader.readexactly(1), timeout=timeout))[0]
            await asyncio.wait_for(reader.readexactly(domain_len + 2), timeout=timeout)
        else:
            raise ConnectionError(f"Unsupported SOCKS5 ATYP: 0x{atyp:02x}")

        return reader, writer
    except BaseException:
        writer.close()
        with contextlib.suppress(OSError):
            await writer.wait_closed()
        raise


def resolve_usque_binary(data_dir: Path | None = None) -> Path | None:
    """Locate usque.exe, extracting from PyInstaller bundle into %LOCALAPPDATA%\\Relay\\bin when frozen."""
    base = data_dir or default_data_dir()
    installed = base / "bin" / "usque.exe"

    if getattr(sys, "frozen", False):
        bundled = Path(getattr(sys, "_MEIPASS", "")) / "relay_bin" / "usque.exe"
        if bundled.is_file():
            try:
                if not installed.is_file() or installed.stat().st_size != bundled.stat().st_size:
                    installed.parent.mkdir(parents=True, exist_ok=True)
                    tmp = installed.with_suffix(".tmp")
                    shutil.copy2(bundled, tmp)
                    tmp.replace(installed)
                return installed
            except OSError:
                return bundled

    if installed.is_file():
        return installed

    repo_bin = Path(__file__).resolve().parents[3] / "bin" / "usque.exe"
    if repo_bin.is_file():
        return repo_bin

    return None


def _port_listening(host: str, port: int, timeout: float = 0.25) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


class MasqueTunnel:
    """Manages a local loopback-only usque (Cloudflare WARP MASQUE UDP 443) SOCKS5 subprocess."""

    def __init__(
        self,
        *,
        host: str = "127.0.0.1",
        port: int = TUNNEL_PORT,
        data_dir: Path | None = None,
    ):
        self.host = host
        self.port = port
        self.data_dir = data_dir or default_data_dir()
        self.config_path = self.data_dir / "usque-config.json"
        self.log_path = self.data_dir / "usque.log"
        self._process: subprocess.Popen | None = None

    @property
    def available(self) -> bool:
        return self._process is not None and self._process.poll() is None

    def is_ready(self) -> bool:
        return _port_listening(self.host, self.port, timeout=0.25)

    def _ensure_registered(self, binary: Path) -> bool:
        if self.config_path.is_file() and self.config_path.stat().st_size > 20:
            return True
        self.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            res = subprocess.run(
                [str(binary), "-c", str(self.config_path), "register", "--accept-tos", "--name", "Relay"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=15,
            )
            return res.returncode == 0 and self.config_path.is_file()
        except (OSError, subprocess.SubprocessError):
            return False

    def start(self) -> bool:
        """Start usque socks bound strictly to 127.0.0.1. Returns True if ready."""
        if self.is_ready():
            return True

        binary = resolve_usque_binary(self.data_dir)
        if binary is None:
            return False

        if not self._ensure_registered(binary):
            return False

        self.data_dir.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            if self.log_path.is_file() and self.log_path.stat().st_size > 524_288:
                self.log_path.write_text("", encoding="utf-8")

        cmd = [
            str(binary),
            "-c",
            str(self.config_path),
            "socks",
            "-b",
            self.host,
            "-p",
            str(self.port),
            "--http2",
            "--always-reconnect",
            "-k",
            "10s",
            "-d",
            "1.1.1.1",
            "-d",
            "8.8.8.8",
        ]
        try:
            with self.log_path.open("a", encoding="utf-8") as log_file:
                self._process = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
        except OSError:
            self._process = None
            return False

        for _ in range(30):
            if self.is_ready():
                return True
            if self._process.poll() is not None:
                self._process = None
                return False
            time.sleep(0.15)
        return self.is_ready()

    def stop(self) -> None:
        proc = self._process
        self._process = None
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()

        # Also clean up any orphaned usque listener on self.port if _process wasn't tracked.
        if self.is_ready():
            pids: set[int] = set()
            with contextlib.suppress(Exception):
                out = subprocess.check_output(
                    ["netstat", "-ano", "-p", "TCP"],
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                    timeout=3,
                    text=True,
                )
                target = f"{self.host}:{self.port}"
                for line in out.splitlines():
                    if target in line and "LISTENING" in line:
                        parts = line.split()
                        if parts and parts[-1].isdigit():
                            pids.add(int(parts[-1]))
            taskkill = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe")
            for pid in pids:
                with contextlib.suppress(Exception):
                    subprocess.run(
                        [taskkill, "/PID", str(pid), "/T", "/F"],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                        timeout=5,
                    )
