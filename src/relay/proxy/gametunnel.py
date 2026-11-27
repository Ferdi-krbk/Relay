"""Process-level split-tunnel Game Mode powered by sing-box + wintun.dll.

Safety guarantees:
  1. Wintun adapter (`RelayGameTun`) is ephemeral: Windows destroys it & all its routes
     automatically the instant sing-box.exe exits.
  2. Elevated `--game-tunnel` supervisor assigns sing-box.exe to a Windows Job Object with
     `JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE` so sing-box can never outlive the supervisor.
  3. Authenticated loopback HTTP endpoint (`127.0.0.1:11895/stop`) lets non-elevated Relay
     cleanly shut down the elevated supervisor without a second UAC prompt.
  4. 1-second parent-PID watchdog + `SetConsoleCtrlHandler` guarantee sing-box terminates
     automatically even if Relay crashes or Windows logs off/shuts down.
"""
from __future__ import annotations

import asyncio
import contextlib
import ctypes
from ctypes import wintypes
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import threading
import time
from typing import Iterable
from urllib.request import ProxyHandler, Request, build_opener

from relay.core.config import (
    DEFAULT_GAME_APPS,
    GAME_CONTROL_PORT,
    GAME_SOCKS_SHIM_PORT,
    GAME_TUNNEL_PORT,
    TUNNEL_PORT,
    default_data_dir,
    sanitize_app_name,
)
from relay.proxy.tunnel import MasqueTunnel


# Networks that must NEVER be captured by the Wintun default route:
# - Loopback & RFC1918 LAN (prevents local/Kaspersky/router loops)
# - Cloudflare MASQUE endpoints (162.159.192.0/21 covers 162.159.198.2 etc.)
# - Public DoH resolvers (1.1.1.1, 1.0.0.1, 8.8.8.8, 8.8.4.4, 9.9.9.9)
_TUN_EXCLUDE_CIDRS: list[str] = [
    "127.0.0.0/8",
    "10.0.0.0/8",
    "172.16.0.0/12",
    "192.168.0.0/16",
    "169.254.0.0/16",
    "162.159.192.0/21",
    "1.1.1.1/32",
    "1.0.0.1/32",
    "8.8.8.8/32",
    "8.8.4.4/32",
    "9.9.9.9/32",
    "9.9.9.10/32",
    "149.112.112.112/32",
]


def _direct_opener():
    return build_opener(ProxyHandler({}))


class Socks5UdpShim:
    """Loopback SOCKS5 proxy shim that fixes usque's strict UDP ASSOCIATE port check.

    sing-box sends `DST.ADDR = 0.0.0.0:0` on SOCKS5 UDP ASSOCIATE, which usque's
    SOCKS5 server rejects (`udp address ... is not associated with tcp`).
    This shim binds a real local UDP socket per association and registers that exact
    `(127.0.0.1, port)` with usque, while transparently splicing TCP CONNECT streams.
    """

    def __init__(self, listen_port: int, upstream_port: int):
        self.listen_port = int(listen_port)
        self.upstream_port = int(upstream_port)
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._stop_fut: asyncio.Future | None = None
        self.bound_port: int = self.listen_port

    def start(self) -> int:
        if self._thread is not None and self._thread.is_alive():
            return self.bound_port
        self._ready.clear()
        self._thread = threading.Thread(target=self._run, name="relay-socks5-shim", daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=3.0):
            raise RuntimeError("SOCKS5 UDP köprüsü başlatılamadı.")
        return self.bound_port

    def stop(self) -> None:
        loop = self._loop
        fut = self._stop_fut
        if loop is not None and fut is not None and not loop.is_closed():
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(lambda: fut.done() or fut.set_result(None))
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def _run(self) -> None:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self._loop = loop
        try:
            loop.run_until_complete(self._serve())
        except Exception:
            self._ready.set()
        finally:
            with contextlib.suppress(Exception):
                pending = asyncio.all_tasks(loop)
                for t in pending:
                    t.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
                loop.close()

    async def _serve(self) -> None:
        loop = asyncio.get_running_loop()
        self._stop_fut = loop.create_future()
        server = await asyncio.start_server(self._handle_client, "127.0.0.1", self.listen_port)
        sock = server.sockets[0]
        self.bound_port = int(sock.getsockname()[1])
        self._ready.set()
        try:
            await self._stop_fut
        finally:
            server.close()
            if hasattr(server, "close_clients"):
                server.close_clients()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(server.wait_closed(), timeout=1.0)

    @staticmethod
    async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                writer.write(data)
                await writer.drain()
        except Exception:
            pass
        finally:
            with contextlib.suppress(Exception):
                writer.close()

    @staticmethod
    async def _read_socks_addr(reader: asyncio.StreamReader, atyp: int) -> bytes:
        if atyp == 1:
            return await reader.readexactly(4 + 2)
        if atyp == 3:
            ln = (await reader.readexactly(1))[0]
            return bytes([ln]) + await reader.readexactly(ln + 2)
        if atyp == 4:
            return await reader.readexactly(16 + 2)
        raise ValueError(f"unsupported atyp {atyp}")

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        up_writer: asyncio.StreamWriter | None = None
        client_udp_tr = None
        up_udp_tr = None
        try:
            hdr = await asyncio.wait_for(reader.readexactly(2), timeout=5.0)
            if hdr[0] != 5:
                return
            await asyncio.wait_for(reader.readexactly(hdr[1]), timeout=5.0)
            writer.write(b"\x05\x00")
            await writer.drain()

            req_hdr = await asyncio.wait_for(reader.readexactly(4), timeout=5.0)
            ver, cmd, _rsv, atyp = req_hdr
            if ver != 5:
                return
            addr_bytes = await asyncio.wait_for(self._read_socks_addr(reader, atyp), timeout=5.0)

            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self.upstream_port), timeout=5.0
            )
            up_writer.write(b"\x05\x01\x00")
            await up_writer.drain()
            u_greet = await asyncio.wait_for(up_reader.readexactly(2), timeout=5.0)
            if u_greet != b"\x05\x00":
                writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return

            if cmd == 1:  # TCP CONNECT — splice transparently
                up_writer.write(req_hdr + addr_bytes)
                await up_writer.drain()
                t1 = asyncio.create_task(self._pipe(reader, up_writer))
                t2 = asyncio.create_task(self._pipe(up_reader, writer))
                await asyncio.gather(t1, t2, return_exceptions=True)
                return

            if cmd != 3:  # Only CONNECT and UDP ASSOCIATE supported
                writer.write(b"\x05\x07\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return

            # UDP ASSOCIATE: bind dedicated local UDP sockets and register exact port with usque
            loop = asyncio.get_running_loop()
            state: dict[str, tuple[str, int] | None] = {"client_addr": None, "usque_addr": None}

            class _ClientUdp(asyncio.DatagramProtocol):
                def datagram_received(self, data: bytes, addr):
                    state["client_addr"] = addr
                    target = state["usque_addr"]
                    if target is not None and up_udp_tr is not None:
                        up_udp_tr.sendto(data, target)

            class _UpstreamUdp(asyncio.DatagramProtocol):
                def datagram_received(self, data: bytes, _addr):
                    c_addr = state["client_addr"]
                    if c_addr is not None and client_udp_tr is not None:
                        client_udp_tr.sendto(data, c_addr)

            client_udp_tr, _ = await loop.create_datagram_endpoint(
                _ClientUdp, local_addr=("127.0.0.1", 0)
            )
            up_udp_tr, _ = await loop.create_datagram_endpoint(
                _UpstreamUdp, local_addr=("127.0.0.1", 0)
            )

            c_port = int(client_udp_tr.get_extra_info("sockname")[1])
            u_port = int(up_udp_tr.get_extra_info("sockname")[1])

            up_writer.write(b"\x05\x03\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", u_port))
            await up_writer.drain()

            u_rep_hdr = await asyncio.wait_for(up_reader.readexactly(4), timeout=5.0)
            u_addr_raw = await asyncio.wait_for(self._read_socks_addr(up_reader, u_rep_hdr[3]), timeout=5.0)
            if u_rep_hdr[1] != 0:
                writer.write(b"\x05\x01\x00\x01\x00\x00\x00\x00\x00\x00")
                await writer.drain()
                return

            if u_rep_hdr[3] == 1:
                usque_ip = socket.inet_ntoa(u_addr_raw[:4])
                usque_port = struct.unpack("!H", u_addr_raw[4:6])[0]
                if usque_ip == "0.0.0.0":
                    usque_ip = "127.0.0.1"
            else:
                usque_ip = "127.0.0.1"
                usque_port = self.upstream_port
            state["usque_addr"] = (usque_ip, usque_port)

            writer.write(b"\x05\x00\x00\x01" + socket.inet_aton("127.0.0.1") + struct.pack("!H", c_port))
            await writer.drain()

            # Keep UDP association alive until either TCP control socket closes
            c_wait = asyncio.create_task(reader.read(64))
            u_wait = asyncio.create_task(up_reader.read(64))
            done, pending = await asyncio.wait({c_wait, u_wait}, return_when=asyncio.FIRST_COMPLETED)
            for p in pending:
                p.cancel()
        except Exception:
            pass
        finally:
            for tr in (client_udp_tr, up_udp_tr):
                if tr is not None:
                    with contextlib.suppress(Exception):
                        tr.close()
            for w in (up_writer, writer):
                if w is not None:
                    with contextlib.suppress(Exception):
                        w.close()


def build_singbox_config(
    apps: Iterable[str],
    *,
    socks_port: int = GAME_SOCKS_SHIM_PORT,
    interface_name: str = "RelayGameTun",
) -> dict:
    """Build a sing-box v1.12+ split-tunnel config routing ONLY listed apps via MASQUE SOCKS5."""
    cleaned = sorted({a for a in (sanitize_app_name(x) for x in apps) if a}, key=str.lower)
    if not cleaned:
        cleaned = list(DEFAULT_GAME_APPS)

    # Critical: never route Relay's own processes back into the TUN (prevents routing loops).
    self_procs = ["usque.exe", "Relay.exe", "sing-box.exe", "python.exe", "pythonw.exe"]

    return {
        "log": {"level": "warn", "timestamp": True},
        "dns": {
            "servers": [
                {
                    "tag": "doh",
                    "type": "https",
                    "server": "1.1.1.1",
                    "server_port": 443,
                    "path": "/dns-query",
                    "tls": {"enabled": True, "server_name": "cloudflare-dns.com"},
                },
                {"tag": "local", "type": "local"},
            ],
            "rules": [
                {"process_name": self_procs, "server": "local"},
                {"process_name": cleaned, "server": "doh"},
            ],
            "final": "local",
        },
        "inbounds": [
            {
                "type": "tun",
                "tag": "tun-in",
                "interface_name": interface_name,
                "address": ["172.19.0.1/30"],
                "mtu": 1400,
                "auto_route": True,
                "strict_route": False,
                "route_exclude_address": list(_TUN_EXCLUDE_CIDRS),
                "stack": "gvisor",
            }
        ],
        "outbounds": [
            {"type": "direct", "tag": "direct"},
            {
                "type": "socks",
                "tag": "usque-socks",
                "server": "127.0.0.1",
                "server_port": int(socks_port),
                "version": "5",
                "udp_over_tcp": False,
            },
        ],
        "route": {
            "auto_detect_interface": True,
            "default_domain_resolver": "local",
            "rules": [
                {"action": "sniff"},
                {"protocol": "dns", "action": "hijack-dns"},
                {"process_name": self_procs, "action": "route", "outbound": "direct"},
                {"ip_is_private": True, "action": "route", "outbound": "direct"},
                {"process_name": cleaned, "action": "route", "outbound": "usque-socks"},
            ],
            "final": "direct",
        },
    }


def resolve_game_binaries(data_dir: Path | None = None) -> tuple[Path, Path] | None:
    """Locate (sing-box.exe, wintun.dll), extracting from PyInstaller bundle when frozen."""
    base = data_dir or default_data_dir()
    bin_dir = base / "bin"
    sb_installed = bin_dir / "sing-box.exe"
    wt_installed = bin_dir / "wintun.dll"

    if getattr(sys, "frozen", False):
        bundled_dir = Path(getattr(sys, "_MEIPASS", "")) / "relay_bin"
        for name, target in (("sing-box.exe", sb_installed), ("wintun.dll", wt_installed)):
            src = bundled_dir / name
            if src.is_file():
                with contextlib.suppress(OSError):
                    if not target.is_file() or target.stat().st_size != src.stat().st_size:
                        target.parent.mkdir(parents=True, exist_ok=True)
                        tmp = target.with_suffix(".tmp")
                        shutil.copy2(src, tmp)
                        tmp.replace(target)

    if sb_installed.is_file() and wt_installed.is_file():
        return sb_installed, wt_installed

    repo_bin = Path(__file__).resolve().parents[3] / "bin"
    sb_repo = repo_bin / "sing-box.exe"
    wt_repo = repo_bin / "wintun.dll"
    if sb_repo.is_file() and wt_repo.is_file():
        return sb_repo, wt_repo
    return None


def _is_pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    SYNCHRONIZE = 0x00100000
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.OpenProcess(SYNCHRONIZE, False, pid)
    if not handle:
        return False
    try:
        return kernel32.WaitForSingleObject(handle, 0) == 0x00000102  # WAIT_TIMEOUT -> still running
    finally:
        kernel32.CloseHandle(handle)


def _create_kill_on_close_job() -> int | None:
    """Create a Windows Job Object that terminates all child processes when closed."""
    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        job = kernel32.CreateJobObjectW(None, None)
        if not job:
            return None

        class JOBOBJECT_BASIC_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class IO_COUNTERS(ctypes.Structure):
            _fields_ = [(n, ctypes.c_uint64) for n in ("ReadOperationCount", "WriteOperationCount", "OtherOperationCount", "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

        class JOBOBJECT_EXTENDED_LIMIT_INFORMATION(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", JOBOBJECT_BASIC_LIMIT_INFORMATION),
                ("IoInfo", IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = JOBOBJECT_EXTENDED_LIMIT_INFORMATION()
        info.BasicLimitInformation.LimitFlags = 0x00002000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = kernel32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info))
        if not ok:
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except Exception:
        return None


def _assign_pid_to_job(job_handle: int, pid: int) -> None:
    try:
        PROCESS_SET_QUOTA = 0x0100
        PROCESS_TERMINATE = 0x0001
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        proc = kernel32.OpenProcess(PROCESS_SET_QUOTA | PROCESS_TERMINATE, False, pid)
        if proc:
            try:
                kernel32.AssignProcessToJobObject(wintypes.HANDLE(job_handle), proc)
            finally:
                kernel32.CloseHandle(proc)
    except Exception:
        pass


class GameTunnelClient:
    """Non-elevated controller for launching, querying, and cleanly stopping the elevated game tunnel."""

    def __init__(
        self,
        data_dir: Path | None = None,
        *,
        control_port: int = GAME_CONTROL_PORT,
        socks_port: int = GAME_SOCKS_SHIM_PORT,
    ):
        self.data_dir = data_dir or default_data_dir()
        self.control_port = control_port
        self.socks_port = socks_port
        self._token_file = self.data_dir / "control.token"

    def _read_token(self) -> str:
        with contextlib.suppress(OSError):
            if self._token_file.is_file():
                return self._token_file.read_text(encoding="utf-8").strip()
        return ""

    def health(self) -> dict | None:
        try:
            with _direct_opener().open(f"http://127.0.0.1:{self.control_port}/health", timeout=0.35) as resp:
                data = json.load(resp)
                if isinstance(data, dict) and data.get("ok") is True:
                    return data
        except (OSError, ValueError):
            pass
        return None

    def is_running(self) -> bool:
        return self.health() is not None

    def stop(self) -> bool:
        """Request clean immediate shutdown of the elevated game-tunnel supervisor & sing-box."""
        if not self.is_running():
            return True
        token = self._read_token()
        req = Request(
            f"http://127.0.0.1:{self.control_port}/stop",
            method="POST",
            data=b"{}",
            headers={"X-Relay-Token": token, "Content-Type": "application/json"},
        )
        with contextlib.suppress(OSError, ValueError):
            with _direct_opener().open(req, timeout=2.0):
                pass
        for _ in range(30):
            if not self.is_running():
                return True
            time.sleep(0.1)
        return not self.is_running()

    def start(self, apps: Iterable[str]) -> bool:
        """Write sing-box config and launch elevated `--game-tunnel` supervisor via UAC."""
        if self.is_running():
            self.stop()

        bins = resolve_game_binaries(self.data_dir)
        if bins is None:
            raise RuntimeError("Oyun Modu dosyaları (sing-box.exe / wintun.dll) bulunamadı.")

        self.data_dir.mkdir(parents=True, exist_ok=True)
        cfg_path = self.data_dir / "singbox-game.json"
        cfg = build_singbox_config(apps, socks_port=self.socks_port)
        tmp = cfg_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(cfg_path)

        token = self._read_token()
        parent_pid = os.getpid()
        params = [
            "--game-tunnel",
            "--config",
            str(cfg_path),
            "--control-port",
            str(self.control_port),
            "--parent-pid",
            str(parent_pid),
            "--token",
            token,
        ]

        if getattr(sys, "frozen", False):
            exe = sys.executable
            arg_str = subprocess.list2cmdline(params)
        else:
            exe = sys.executable
            arg_str = subprocess.list2cmdline(["-m", "relay", *params])

        # Launch elevated via ShellExecuteW("runas") with SW_HIDE (0).
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        rc = shell32.ShellExecuteW(None, "runas", exe, arg_str, str(bins[0].parent), 0)
        if rc <= 32:
            raise RuntimeError("Oyun Modu için Yönetici izni iptal edildi veya başlatılamadı.")

        for _ in range(50):
            if self.is_running():
                return True
            time.sleep(0.15)
        raise RuntimeError("Oyun Modu tüneli zamanında başlamadı (singbox-game.log kontrol et).")


def run_game_tunnel_supervisor(args) -> None:
    """Elevated entry point: runs sing-box inside a KILL_ON_JOB_CLOSE job with HTTP /stop + parent watchdog."""
    data_dir = default_data_dir()
    bins = resolve_game_binaries(data_dir)
    if bins is None:
        return
    singbox_exe, _ = bins
    cfg_path = Path(args.config)
    control_port = int(args.control_port)
    parent_pid = int(args.parent_pid)
    expected_token = str(args.token or "")

    stop_event = threading.Event()
    job_handle = _create_kill_on_close_job()

    log_path = data_dir / "singbox-game.log"
    with contextlib.suppress(OSError):
        if log_path.is_file() and log_path.stat().st_size > 524_288:
            log_path.write_text("", encoding="utf-8")

    # Register Windows logoff/shutdown handler
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    def _on_ctrl(_ctrl_type: int) -> bool:
        stop_event.set()
        return False

    handler_ref = handler_type(_on_ctrl)
    with contextlib.suppress(Exception):
        ctypes.windll.kernel32.SetConsoleCtrlHandler(handler_ref, True)

    log_file = log_path.open("a", encoding="utf-8")
    proc: subprocess.Popen | None = None
    shim: Socks5UdpShim | None = None
    game_masque: MasqueTunnel | None = None
    try:
        # If sing-box config points to our local SOCKS5 UDP shim port (different from TUNNEL_PORT),
        # run a dedicated isolated MasqueTunnel on GAME_TUNNEL_PORT so heavy game UDP/TCP traffic
        # never shares or head-of-line blocks the Web Proxy's MASQUE connection on TUNNEL_PORT.
        shim_port = 0
        with contextlib.suppress(OSError, ValueError, KeyError):
            parsed_cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
            for ob in parsed_cfg.get("outbounds", []):
                if ob.get("tag") == "usque-socks" and ob.get("type") == "socks":
                    p = int(ob.get("server_port", 0))
                    if p > 0 and p != TUNNEL_PORT:
                        shim_port = p
                    break
        if shim_port:
            upstream_socks = TUNNEL_PORT
            gm = MasqueTunnel(host="127.0.0.1", port=GAME_TUNNEL_PORT, data_dir=data_dir)
            if gm.start():
                game_masque = gm
                upstream_socks = GAME_TUNNEL_PORT
                if job_handle and gm._process is not None:
                    _assign_pid_to_job(job_handle, gm._process.pid)

                def _prewarm_game_masque():
                    with contextlib.suppress(Exception):
                        with socket.create_connection(("127.0.0.1", GAME_TUNNEL_PORT), timeout=10.0) as s:
                            s.settimeout(10.0)
                            s.sendall(b"\x05\x01\x00")
                            if s.recv(2) == b"\x05\x00":
                                s.sendall(b"\x05\x01\x00\x01\x01\x01\x01\x01\x01\xbb")
                                s.recv(10)

                threading.Thread(target=_prewarm_game_masque, daemon=True).start()
            shim = Socks5UdpShim(listen_port=shim_port, upstream_port=upstream_socks)
            shim.start()

        proc = subprocess.Popen(
            [str(singbox_exe), "run", "-c", str(cfg_path)],
            cwd=str(singbox_exe.parent),
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        if job_handle:
            _assign_pid_to_job(job_handle, proc.pid)

        class _Handler(BaseHTTPRequestHandler):
            def log_message(self, format, *a):
                return

            def _send(self, code: int, payload: dict) -> None:
                body = json.dumps(payload).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/health" and proc is not None and proc.poll() is None:
                    self._send(200, {"ok": True, "pid": os.getpid(), "singbox_pid": proc.pid})
                else:
                    self._send(503, {"ok": False})

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                if 0 < length <= 4096:
                    self.rfile.read(length)
                if self.path != "/stop":
                    self._send(404, {"ok": False})
                    return
                if self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site"):
                    self._send(403, {"ok": False})
                    return
                supplied = self.headers.get("X-Relay-Token", "")
                if expected_token and (not supplied or not hmac.compare_digest(supplied, expected_token)):
                    self._send(403, {"ok": False})
                    return
                stop_event.set()
                self._send(200, {"ok": True, "stopped": True})

        httpd = ThreadingHTTPServer(("127.0.0.1", control_port), _Handler)
        httpd.timeout = 0.4
        server_thread = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.25}, daemon=True)
        server_thread.start()

        try:
            while not stop_event.is_set():
                if proc.poll() is not None:
                    break
                if parent_pid > 0 and not _is_pid_alive(parent_pid):
                    break
                time.sleep(0.5)
        finally:
            httpd.shutdown()
            httpd.server_close()
    finally:
        if proc is not None and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
            try:
                proc.wait(timeout=2.5)
            except subprocess.TimeoutExpired:
                with contextlib.suppress(OSError):
                    proc.kill()
        if shim is not None:
            with contextlib.suppress(Exception):
                shim.stop()
        if game_masque is not None:
            with contextlib.suppress(Exception):
                game_masque.stop()
        if job_handle:
            with contextlib.suppress(Exception):
                ctypes.WinDLL("kernel32").CloseHandle(wintypes.HANDLE(job_handle))
        with contextlib.suppress(OSError):
            log_file.close()
