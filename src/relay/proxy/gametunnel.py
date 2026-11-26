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

import contextlib
import ctypes
from ctypes import wintypes
import hmac
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading
import time
from typing import Iterable
from urllib.request import ProxyHandler, Request, build_opener

from relay.core.config import (
    DEFAULT_GAME_APPS,
    GAME_CONTROL_PORT,
    TUNNEL_PORT,
    default_data_dir,
    sanitize_app_name,
)


def _direct_opener():
    return build_opener(ProxyHandler({}))


def build_singbox_config(
    apps: Iterable[str],
    *,
    socks_port: int = TUNNEL_PORT,
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
                "stack": "mixed",
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
        socks_port: int = TUNNEL_PORT,
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
    try:
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
        if job_handle:
            with contextlib.suppress(Exception):
                ctypes.WinDLL("kernel32").CloseHandle(wintypes.HANDLE(job_handle))
        with contextlib.suppress(OSError):
            log_file.close()
