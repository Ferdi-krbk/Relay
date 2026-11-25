"""Background proxy worker subprocess lifecycle and local HTTP control client."""
from __future__ import annotations

import contextlib
import json
import os
from pathlib import Path
import secrets
import subprocess
import sys
import time
from typing import Protocol
from urllib.request import ProxyHandler, Request, build_opener

from relay.core.config import RelayConfig, default_data_dir


class WorkerProcessManager(Protocol):
    def health(self, port: int) -> dict | None: ...
    def stats(self, port: int) -> dict | None: ...
    def is_ready(self, port: int) -> bool: ...
    def start(self, port: int, config: RelayConfig, *, force_restart: bool = False) -> None: ...
    def stop(self, port: int) -> None: ...
    def update_config(self, port: int, config: RelayConfig) -> bool: ...
    def flush(self, port: int, *, clear_persisted: bool = False) -> bool: ...


def _direct_opener():
    return build_opener(ProxyHandler({}))


class LocalWorkerManager:
    """Manages spawning, health-checking, hot-reloading, and cleanly terminating the local proxy worker."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or default_data_dir()
        self._processes: dict[int, subprocess.Popen] = {}
        self._token_file = self.data_dir / "control.token"

    def _get_or_create_token(self) -> str:
        with contextlib.suppress(OSError):
            if self._token_file.is_file():
                existing = self._token_file.read_text(encoding="utf-8").strip()
                if len(existing) >= 16:
                    return existing
        token = secrets.token_hex(16)
        with contextlib.suppress(OSError):
            self.data_dir.mkdir(parents=True, exist_ok=True)
            tmp = self._token_file.with_suffix(".tmp")
            tmp.write_text(token, encoding="utf-8")
            tmp.replace(self._token_file)
        return token

    def _control_post(self, port: int, path: str, payload: dict | None = None, timeout: float = 0.6) -> dict | None:
        token = self._get_or_create_token()
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else b""
        headers = {"X-Relay-Token": token}
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        req = Request(f"http://127.0.0.1:{port}{path}", method="POST", data=body, headers=headers)
        try:
            with _direct_opener().open(req, timeout=timeout) as resp:
                data = json.load(resp)
                if isinstance(data, dict) and data.get("ok") is True:
                    return data
        except (OSError, ValueError):
            pass
        return None

    def update_config(self, port: int, config: RelayConfig) -> bool:
        payload = {
            "mode": config.mode.value,
            "doh_enabled": config.doh_enabled,
            "bypass_domains": list(config.bypass_domains),
            "force_fragment_domains": list(config.force_fragment_domains),
        }
        return self._control_post(port, "/config", payload) is not None

    def flush(self, port: int, *, clear_persisted: bool = False) -> bool:
        return self._control_post(port, "/flush", {"clear_persisted": clear_persisted}) is not None

    def health(self, port: int) -> dict | None:
        try:
            with _direct_opener().open(f"http://127.0.0.1:{port}/health", timeout=0.25) as resp:
                data = json.load(resp)
                if isinstance(data, dict) and data.get("ok") is True:
                    return data
        except (OSError, ValueError):
            pass
        return None

    def stats(self, port: int) -> dict | None:
        try:
            with _direct_opener().open(f"http://127.0.0.1:{port}/stats", timeout=0.4) as resp:
                data = json.load(resp)
                if isinstance(data, dict) and data.get("ok") is True:
                    return data
        except (OSError, ValueError):
            pass
        return None

    def is_ready(self, port: int) -> bool:
        return self.health(port) is not None

    def stop(self, port: int) -> None:
        tracked = self._processes.pop(port, None)
        health = self.health(port)
        if health is None and (tracked is None or tracked.poll() is not None):
            return
        pid = health.get("pid") if isinstance(health, dict) else None

        # 1. Request clean graceful shutdown via POST /shutdown.
        if health is not None:
            self._control_post(port, "/shutdown", timeout=0.5)

            for _ in range(15):
                if not self.is_ready(port):
                    return
                time.sleep(0.08)

        # 2. Fallback: force-terminate tracked or port-owning PID (handles orphaned legacy workers).
        pids: set[int] = set()
        if tracked and tracked.poll() is None:
            pids.add(tracked.pid)
        if isinstance(pid, int) and pid > 0:
            pids.add(pid)
        if not pids:
            with contextlib.suppress(Exception):
                out = subprocess.check_output(
                    ["netstat", "-ano", "-p", "TCP"],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=3,
                    text=True,
                )
                target = f"127.0.0.1:{port}"
                for line in out.splitlines():
                    if target in line and "LISTENING" in line:
                        parts = line.split()
                        if parts and parts[-1].isdigit():
                            pids.add(int(parts[-1]))

        taskkill = str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe")
        for target_pid in pids:
            with contextlib.suppress(Exception):
                subprocess.run(
                    [taskkill, "/PID", str(target_pid), "/T", "/F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    creationflags=subprocess.CREATE_NO_WINDOW,
                    timeout=5,
                )

        for _ in range(15):
            if not self.is_ready(port):
                break
            time.sleep(0.08)

    def start(self, port: int, config: RelayConfig, *, force_restart: bool = False) -> None:
        if force_restart:
            self.stop(port)
        else:
            existing = self.health(port)
            if existing is not None:
                if (
                    existing.get("mode") == config.mode.value
                    and existing.get("doh") == config.doh_enabled
                    and existing.get("pid") is not None
                ):
                    return
                self.stop(port)

        options = [
            "--proxy-worker",
            "--port",
            str(port),
            "--mode",
            config.mode.value,
        ]
        if not config.doh_enabled:
            options.append("--no-doh")
        if config.bypass_domains:
            options.extend(["--bypass", ",".join(config.bypass_domains)])
        if config.force_fragment_domains:
            options.extend(["--force-fragment", ",".join(config.force_fragment_domains)])

        if getattr(sys, "frozen", False):
            command = [sys.executable, *options]
        else:
            command = [sys.executable, "-m", "relay", *options]

        env = dict(
            os.environ,
            PYINSTALLER_RESET_ENVIRONMENT="1",
            RELAY_CONTROL_TOKEN=self._get_or_create_token(),
        )
        if not getattr(sys, "frozen", False):
            src_root = str(Path(__file__).resolve().parents[2])
            existing_pp = env.get("PYTHONPATH", "")
            env["PYTHONPATH"] = f"{src_root}{os.pathsep}{existing_pp}" if existing_pp else src_root

        self.data_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.data_dir / f"worker-{port}.log"
        with contextlib.suppress(OSError):
            if log_path.is_file() and log_path.stat().st_size > 1_048_576:
                log_path.write_text("", encoding="utf-8")

        with log_path.open("a", encoding="utf-8") as log_file:
            process = subprocess.Popen(
                command,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                creationflags=subprocess.CREATE_NO_WINDOW,
                env=env,
            )
        self._processes[port] = process

        for _ in range(125):
            if self.is_ready(port):
                return
            if process.poll() is not None:
                break
            time.sleep(0.12)
        raise RuntimeError(f"Yerel Relay servisi başlatılamadı (port {port}).")
