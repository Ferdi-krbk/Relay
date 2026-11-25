"""Windows Internet Settings registry abstraction, backup/restore, and RunOnce boot guard."""
from __future__ import annotations

import contextlib
import ctypes
import json
from pathlib import Path
import sys
from typing import Protocol
import winreg

from relay.core.config import ALL_MANAGED_PORTS, default_data_dir

SETTINGS_KEY = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
RUNONCE_KEY = r"Software\Microsoft\Windows\CurrentVersion\RunOnce"
RUNONCE_NAME = "RelayRestoreProxy"
TRACKED_VALUES: tuple[str, ...] = ("ProxyEnable", "AutoConfigURL", "ProxyServer", "ProxyOverride")
PROXY_OVERRIDE = "localhost;127.0.0.1;127.*;[::1];::1;10.*;172.16.*;192.168.*;<local>"


class ProxySettingsStore(Protocol):
    def active_port(self) -> int | None: ...
    def capture_baseline(self) -> list[dict]: ...
    def load_baseline(self) -> list[dict]: ...
    def save_baseline(self, baseline: list[dict]) -> None: ...
    def clear_baseline(self) -> None: ...
    def apply_proxy(self, port: int, *, system_proxy: bool) -> None: ...
    def restore_baseline(self, baseline: list[dict]) -> None: ...
    def set_boot_safety_net(self, enabled: bool) -> None: ...


def pac_url_for_port(port: int) -> str:
    return f"http://127.0.0.1:{port}/proxy.pac"


def notify_windows_network_changed() -> None:
    """Broadcast WinInet proxy settings change so browsers/apps pick it up immediately."""
    wininet = ctypes.WinDLL("wininet", use_last_error=True)
    wininet.InternetSetOptionW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    for option in (39, 37):  # INTERNET_OPTION_SETTINGS_CHANGED, INTERNET_OPTION_REFRESH
        wininet.InternetSetOptionW(None, option, None, 0)


def safe_clean_baseline() -> list[dict]:
    """Safe direct-internet fallback if no backup file exists."""
    return [
        {"Name": "ProxyEnable", "Exists": True, "Value": 0, "Kind": winreg.REG_DWORD},
        {"Name": "AutoConfigURL", "Exists": False},
        {"Name": "ProxyServer", "Exists": False},
        {"Name": "ProxyOverride", "Exists": False},
    ]


class WindowsProxyStore:
    """Concrete Windows HKCU registry + WinInet implementation of ProxySettingsStore."""

    def __init__(self, data_dir: Path | None = None):
        self.data_dir = data_dir or default_data_dir()
        self.baseline_file = self.data_dir / "browser-settings.json"

    def active_port(self) -> int | None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
            try:
                url = winreg.QueryValueEx(key, "AutoConfigURL")[0]
                for port in ALL_MANAGED_PORTS:
                    if pac_url_for_port(port) == url:
                        return port
            except FileNotFoundError:
                pass
            try:
                enabled = winreg.QueryValueEx(key, "ProxyEnable")[0]
                server = winreg.QueryValueEx(key, "ProxyServer")[0]
                if enabled == 1 and isinstance(server, str):
                    for port in ALL_MANAGED_PORTS:
                        if f"127.0.0.1:{port}" in server:
                            return port
            except FileNotFoundError:
                pass
        return None

    def capture_baseline(self) -> list[dict]:
        result: list[dict] = []
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY) as key:
            for name in TRACKED_VALUES:
                try:
                    value, kind = winreg.QueryValueEx(key, name)
                    result.append({"Name": name, "Exists": True, "Value": value, "Kind": kind})
                except FileNotFoundError:
                    result.append({"Name": name, "Exists": False})
        return result

    def save_baseline(self, baseline: list[dict]) -> None:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        tmp = self.baseline_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(baseline, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.baseline_file)

    def load_baseline(self) -> list[dict]:
        if self.baseline_file.is_file():
            try:
                data = json.loads(self.baseline_file.read_text(encoding="utf-8-sig"))
                if isinstance(data, list):
                    return data
            except (OSError, ValueError):
                pass
        return safe_clean_baseline()

    def clear_baseline(self) -> None:
        self.baseline_file.unlink(missing_ok=True)

    def apply_proxy(self, port: int, *, system_proxy: bool) -> None:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            if system_proxy:
                # When full System Proxy is enabled, do not also set AutoConfigURL:
                # Chromium/Electron apps (e.g. Antigravity Hub) race cold-start PAC fetches
                # against local https://127.0.0.1:<port>/ loads and fail with ERR_FAILED (-2).
                with contextlib.suppress(FileNotFoundError):
                    cur_pac = winreg.QueryValueEx(key, "AutoConfigURL")[0]
                    if isinstance(cur_pac, str) and "127.0.0.1:" in cur_pac and cur_pac.endswith("/proxy.pac"):
                        winreg.DeleteValue(key, "AutoConfigURL")
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, f"127.0.0.1:{port}")
                winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, PROXY_OVERRIDE)
            else:
                winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, pac_url_for_port(port))
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                with contextlib.suppress(FileNotFoundError):
                    cur_srv = winreg.QueryValueEx(key, "ProxyServer")[0]
                    if isinstance(cur_srv, str) and f"127.0.0.1:{port}" in cur_srv:
                        winreg.DeleteValue(key, "ProxyServer")
                        with contextlib.suppress(FileNotFoundError):
                            winreg.DeleteValue(key, "ProxyOverride")
        notify_windows_network_changed()

    def restore_baseline(self, baseline: list[dict]) -> None:
        kinds = {"DWord": winreg.REG_DWORD, "String": winreg.REG_SZ, "ExpandString": winreg.REG_EXPAND_SZ}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY, 0, winreg.KEY_SET_VALUE) as key:
            for item in baseline:
                name = item.get("Name")
                if name not in TRACKED_VALUES:
                    continue
                if item.get("Exists"):
                    kind = item["Kind"]
                    winreg.SetValueEx(key, name, 0, kinds.get(kind, kind), item["Value"])
                else:
                    with contextlib.suppress(FileNotFoundError):
                        winreg.DeleteValue(key, name)
        notify_windows_network_changed()

    def set_boot_safety_net(self, enabled: bool) -> None:
        """Never write RunOnce keys (triggers AV heuristic blockers like Kaspersky System Watcher).
        Only removes any legacy RunOnce entry if present."""
        with contextlib.suppress(OSError):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUNONCE_KEY, 0, winreg.KEY_SET_VALUE) as key:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, RUNONCE_NAME)
