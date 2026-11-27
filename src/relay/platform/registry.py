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

INTERNET_OPTION_REFRESH = 37
INTERNET_OPTION_SETTINGS_CHANGED = 39
INTERNET_OPTION_PER_CONNECTION_OPTION = 75

INTERNET_PER_CONN_FLAGS = 1
INTERNET_PER_CONN_PROXY_SERVER = 2
INTERNET_PER_CONN_PROXY_BYPASS = 3
INTERNET_PER_CONN_AUTOCONFIG_URL = 4

PROXY_TYPE_DIRECT = 0x00000001
PROXY_TYPE_PROXY = 0x00000002
PROXY_TYPE_AUTO_PROXY_URL = 0x00000004


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _INTERNET_PER_CONN_OPTION_VALUE(ctypes.Union):
    _fields_ = [
        ("dwValue", ctypes.c_uint32),
        ("pszValue", ctypes.c_wchar_p),
        ("ftValue", _FILETIME),
    ]


class _INTERNET_PER_CONN_OPTIONW(ctypes.Structure):
    _fields_ = [
        ("dwOption", ctypes.c_uint32),
        ("Value", _INTERNET_PER_CONN_OPTION_VALUE),
    ]


class _INTERNET_PER_CONN_OPTION_LISTW(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.c_uint32),
        ("pszConnection", ctypes.c_wchar_p),
        ("dwOptionCount", ctypes.c_uint32),
        ("dwOptionError", ctypes.c_uint32),
        ("pOptions", ctypes.POINTER(_INTERNET_PER_CONN_OPTIONW)),
    ]


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


def _apply_wininet_per_connection(
    *,
    flags: int,
    proxy_server: str = "",
    proxy_bypass: str = "",
    autoconfig_url: str = "",
) -> None:
    """Synchronize WinINet Connections\\DefaultConnectionSettings & SavedLegacySettings."""
    opts = (_INTERNET_PER_CONN_OPTIONW * 4)()
    opts[0].dwOption = INTERNET_PER_CONN_FLAGS
    opts[0].Value.dwValue = int(flags)
    opts[1].dwOption = INTERNET_PER_CONN_PROXY_SERVER
    opts[1].Value.pszValue = proxy_server
    opts[2].dwOption = INTERNET_PER_CONN_PROXY_BYPASS
    opts[2].Value.pszValue = proxy_bypass
    opts[3].dwOption = INTERNET_PER_CONN_AUTOCONFIG_URL
    opts[3].Value.pszValue = autoconfig_url

    opt_list = _INTERNET_PER_CONN_OPTION_LISTW()
    opt_list.dwSize = ctypes.sizeof(_INTERNET_PER_CONN_OPTION_LISTW)
    opt_list.pszConnection = None
    opt_list.dwOptionCount = 4
    opt_list.dwOptionError = 0
    opt_list.pOptions = opts

    wininet = ctypes.WinDLL("wininet", use_last_error=True)
    wininet.InternetSetOptionW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    wininet.InternetSetOptionW.restype = ctypes.c_int
    wininet.InternetSetOptionW(
        None,
        INTERNET_OPTION_PER_CONNECTION_OPTION,
        ctypes.byref(opt_list),
        ctypes.sizeof(opt_list),
    )


def notify_windows_network_changed() -> None:
    """Broadcast WinInet proxy settings change so browsers/apps pick it up immediately."""
    wininet = ctypes.WinDLL("wininet", use_last_error=True)
    wininet.InternetSetOptionW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_void_p, ctypes.c_uint]
    for option in (INTERNET_OPTION_SETTINGS_CHANGED, INTERNET_OPTION_REFRESH):
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
        pac_url = pac_url_for_port(port)
        proxy_addr = f"127.0.0.1:{port}"
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY, 0, winreg.KEY_SET_VALUE | winreg.KEY_QUERY_VALUE) as key:
            if system_proxy:
                # When full System Proxy is enabled, do not also set AutoConfigURL:
                # Chromium/Electron apps race cold-start PAC fetches against local loads.
                with contextlib.suppress(FileNotFoundError):
                    cur_pac = winreg.QueryValueEx(key, "AutoConfigURL")[0]
                    if isinstance(cur_pac, str) and "127.0.0.1:" in cur_pac and cur_pac.endswith("/proxy.pac"):
                        winreg.DeleteValue(key, "AutoConfigURL")
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 1)
                winreg.SetValueEx(key, "ProxyServer", 0, winreg.REG_SZ, proxy_addr)
                winreg.SetValueEx(key, "ProxyOverride", 0, winreg.REG_SZ, PROXY_OVERRIDE)
                _apply_wininet_per_connection(
                    flags=PROXY_TYPE_DIRECT | PROXY_TYPE_PROXY,
                    proxy_server=proxy_addr,
                    proxy_bypass=PROXY_OVERRIDE,
                    autoconfig_url="",
                )
            else:
                winreg.SetValueEx(key, "AutoConfigURL", 0, winreg.REG_SZ, pac_url)
                winreg.SetValueEx(key, "ProxyEnable", 0, winreg.REG_DWORD, 0)
                with contextlib.suppress(FileNotFoundError):
                    cur_srv = winreg.QueryValueEx(key, "ProxyServer")[0]
                    if isinstance(cur_srv, str) and proxy_addr in cur_srv:
                        winreg.DeleteValue(key, "ProxyServer")
                        with contextlib.suppress(FileNotFoundError):
                            winreg.DeleteValue(key, "ProxyOverride")
                _apply_wininet_per_connection(
                    flags=PROXY_TYPE_DIRECT | PROXY_TYPE_AUTO_PROXY_URL,
                    proxy_server="",
                    proxy_bypass="",
                    autoconfig_url=pac_url,
                )
        notify_windows_network_changed()

    def restore_baseline(self, baseline: list[dict]) -> None:
        kinds = {"DWord": winreg.REG_DWORD, "String": winreg.REG_SZ, "ExpandString": winreg.REG_EXPAND_SZ}
        restored_map: dict[str, object] = {}
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, SETTINGS_KEY, 0, winreg.KEY_SET_VALUE) as key:
            for item in baseline:
                name = item.get("Name")
                if name not in TRACKED_VALUES:
                    continue
                if item.get("Exists"):
                    kind = item["Kind"]
                    val = item["Value"]
                    # Never restore a stale Relay loopback entry as a baseline.
                    if name == "AutoConfigURL" and isinstance(val, str) and any(
                        pac_url_for_port(p) == val for p in ALL_MANAGED_PORTS
                    ):
                        with contextlib.suppress(FileNotFoundError):
                            winreg.DeleteValue(key, name)
                        continue
                    if name == "ProxyServer" and isinstance(val, str) and any(
                        f"127.0.0.1:{p}" in val for p in ALL_MANAGED_PORTS
                    ):
                        with contextlib.suppress(FileNotFoundError):
                            winreg.DeleteValue(key, name)
                        continue
                    winreg.SetValueEx(key, name, 0, kinds.get(kind, kind), val)
                    restored_map[name] = val
                else:
                    with contextlib.suppress(FileNotFoundError):
                        winreg.DeleteValue(key, name)

        flags = PROXY_TYPE_DIRECT
        proxy_srv = str(restored_map.get("ProxyServer") or "")
        proxy_byp = str(restored_map.get("ProxyOverride") or "")
        auto_url = str(restored_map.get("AutoConfigURL") or "")
        if int(restored_map.get("ProxyEnable") or 0) == 1 and proxy_srv:
            flags |= PROXY_TYPE_PROXY
        else:
            proxy_srv = ""
            proxy_byp = ""
        if auto_url:
            flags |= PROXY_TYPE_AUTO_PROXY_URL
        _apply_wininet_per_connection(
            flags=flags,
            proxy_server=proxy_srv,
            proxy_bypass=proxy_byp,
            autoconfig_url=auto_url,
        )
        notify_windows_network_changed()

    def set_boot_safety_net(self, enabled: bool) -> None:
        """Never write RunOnce keys (triggers AV heuristic blockers like Kaspersky System Watcher).
        Only removes any legacy RunOnce entry if present."""
        with contextlib.suppress(OSError):
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUNONCE_KEY, 0, winreg.KEY_SET_VALUE) as key:
                with contextlib.suppress(FileNotFoundError):
                    winreg.DeleteValue(key, RUNONCE_NAME)
