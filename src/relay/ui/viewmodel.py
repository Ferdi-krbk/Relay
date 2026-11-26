"""MVVM ViewModel exposing RelayService state and commands to Qt Quick."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import ctypes
from ctypes import wintypes
import os
from pathlib import Path

from PySide6.QtCore import Property, QObject, QTimer, Qt, Signal, Slot
from PySide6.QtWidgets import QFileDialog

from relay.core.config import NetworkMode, sanitize_app_name, sanitize_domain
from relay.platform.startup import is_startup_enabled, set_startup_enabled
from relay.service.controller import RelayService, ServiceStatus


_IGNORED_RUNNING_APPS = frozenset(
    {
        "relay.exe",
        "explorer.exe",
        "applicationframehost.exe",
        "textinputhost.exe",
        "systemsettings.exe",
        "shellexperiencehost.exe",
        "searchhost.exe",
        "startmenuexperiencehost.exe",
        "lockapp.exe",
        "nvidia overlay.exe",
    }
)


def list_visible_window_apps() -> list[str]:
    """Return sorted unique .exe names of currently visible desktop windows (zero-subprocess)."""
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        pids: set[int] = set()

        enum_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def _cb(hwnd, _lparam):
            if user32.IsWindowVisible(hwnd) and user32.GetWindowTextLengthW(hwnd) > 0:
                pid = wintypes.DWORD()
                user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                if pid.value:
                    pids.add(int(pid.value))
            return True

        user32.EnumWindows(enum_proc(_cb), 0)

        names: dict[str, str] = {}
        buf = ctypes.create_unicode_buffer(512)
        for pid in pids:
            h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not h:
                continue
            try:
                size = wintypes.DWORD(512)
                if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
                    exe = Path(buf.value).name
                    if exe.lower().endswith(".exe") and exe.lower() not in _IGNORED_RUNNING_APPS:
                        names.setdefault(exe.lower(), exe)
            finally:
                kernel32.CloseHandle(h)
        return sorted(names.values(), key=str.lower)
    except Exception:
        return []


def format_rate(bps: float) -> str:
    if bps < 1024:
        return f"{int(bps)} B/s"
    if bps < 1024 * 1024:
        return f"{bps / 1024:.1f} KB/s"
    return f"{bps / (1024 * 1024):.2f} MB/s"


def format_bytes(total: int) -> str:
    if total < 1024:
        return f"{total} B"
    if total < 1024 * 1024:
        return f"{total / 1024:.1f} KB"
    if total < 1024 * 1024 * 1024:
        return f"{total / (1024 * 1024):.1f} MB"
    return f"{total / (1024 * 1024 * 1024):.2f} GB"


def format_uptime(seconds: int) -> str:
    if seconds < 60:
        return f"{seconds} sn"
    mins, secs = divmod(seconds, 60)
    if mins < 60:
        return f"{mins:02d}:{secs:02d}"
    hours, mins = divmod(mins, 60)
    return f"{hours} sa {mins:02d} dk"


class RelayViewModel(QObject):
    changed = Signal()
    _actionCompleted = Signal(object, str)
    _diagnosticsCompleted = Signal(object)

    def __init__(self, service: RelayService):
        super().__init__()
        self.service = service
        self._executor = ThreadPoolExecutor(max_workers=2)
        self._status = ServiceStatus(active_port=None, ready=False, stats=None)
        self._busy = False
        self._disconnecting = False
        self._error = ""
        self._polling = False
        self._closing = False
        self._testing = False
        self._diagnostic_results: list[dict] = []
        self._peak_bps = 0.0
        self._recovery_attempts = 0
        self._action_seq = 0
        self._start_with_windows = is_startup_enabled()

        self._actionCompleted.connect(self._on_action_done, Qt.ConnectionType.QueuedConnection)
        self._diagnosticsCompleted.connect(self._on_diagnostics_done, Qt.ConnectionType.QueuedConnection)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.poll)
        self.timer.start(1000)
        QTimer.singleShot(0, self.poll)

    # --- Connection State ---
    @Property(bool, notify=changed)
    def connected(self) -> bool:
        return self._status.connected

    @Property(bool, notify=changed)
    def busy(self) -> bool:
        return self._busy

    @Property(bool, notify=changed)
    def disconnecting(self) -> bool:
        return self._busy and self._disconnecting

    @Property(str, notify=changed)
    def error(self) -> str:
        return self._error

    @Property(str, notify=changed)
    def proxyEndpoint(self) -> str:
        port = self._status.active_port or self.service.port
        return f"127.0.0.1:{port}"

    @Property(str, notify=changed)
    def actionText(self) -> str:
        if self._busy:
            return "Kapatılıyor…" if self._disconnecting else "Bağlanıyor…"
        if self.connected:
            return "Bağlantıyı Kes"
        return "Yeniden Bağlan" if self._status.active_port else "Relay'i Başlat"

    # --- Settings ---
    @Property(str, notify=changed)
    def mode(self) -> str:
        return self.service.config.mode.value

    @Property(str, notify=changed)
    def modeLabel(self) -> str:
        return self.service.config.preset.label

    @Property(bool, notify=changed)
    def dohEnabled(self) -> bool:
        return self.service.config.doh_enabled

    @Property(bool, notify=changed)
    def systemProxy(self) -> bool:
        return self.service.config.system_proxy

    @Property(bool, notify=changed)
    def animations(self) -> bool:
        return self.service.config.animations

    @Property(bool, notify=changed)
    def startWithWindows(self) -> bool:
        return self._start_with_windows

    @Property(str, notify=changed)
    def heroSubtitle(self) -> str:
        if self._error:
            return self._error
        if self.connected:
            dns_part = "DoH & MASQUE" if self.service.config.doh_enabled else "Sistem DNS & MASQUE"
            return f"HTTPS trafiği uyarlanabilir TLS, {dns_part} ile yönlendiriliyor."
        return "Tek tıkla tüm HTTPS trafiğini ve şifreli DNS'i Relay üzerinden yönet."

    @Property(str, notify=changed)
    def tunnelLabel(self) -> str:
        if not self.connected:
            return "MASQUE · Beklemede"
        count = self.tunnelCount
        return f"MASQUE · {count} oturum" if count > 0 else "MASQUE · Hazır"

    @Property(str, notify=changed)
    def storesLabel(self) -> str:
        bypass = self.service.config.bypass_domains
        steam_bypassed = any(d == "steampowered.com" or d.endswith(".steampowered.com") for d in bypass)
        epic_bypassed = any(d == "epicgames.com" or d.endswith(".epicgames.com") for d in bypass)
        if steam_bypassed and epic_bypassed:
            return "Steam & Epic · Direkt"
        if steam_bypassed:
            return "Epic · Relay, Steam · Direkt"
        if epic_bypassed:
            return "Steam · Relay, Epic · Direkt"
        return "Steam & Epic · Relay"

    @Property("QVariantList", notify=changed)
    def bypassDomains(self) -> list:
        return list(self.service.config.bypass_domains)

    @Property("QVariantList", notify=changed)
    def forceDomains(self) -> list:
        return list(self.service.config.force_fragment_domains)

    @Property(bool, notify=changed)
    def gameModeActive(self) -> bool:
        return bool(self._status.game_mode)

    @Property("QVariantList", notify=changed)
    def gameApps(self) -> list:
        return list(self.service.config.game_mode_apps)

    @Property("QVariantList", notify=changed)
    def runningApps(self) -> list:
        existing = {a.lower() for a in self.service.config.game_mode_apps}
        return [a for a in getattr(self, "_running_apps", []) if a.lower() not in existing]

    @Slot()
    def refreshRunningApps(self) -> None:
        self._running_apps = list_visible_window_apps()
        self.changed.emit()

    # --- Telemetry ---
    def _stats(self) -> dict:
        return self._status.stats if (self.connected and self._status.stats) else {}

    @Property(int, notify=changed)
    def activeConnections(self) -> int:
        return int(self._stats().get("active_connections", 0))

    @Property(int, notify=changed)
    def totalConnections(self) -> int:
        return int(self._stats().get("total_connections", 0))

    @Property(str, notify=changed)
    def downloadSpeed(self) -> str:
        return format_rate(float(self._stats().get("bps_down", 0.0))) if self.connected else "0 B/s"

    @Property(str, notify=changed)
    def peakSpeed(self) -> str:
        return format_rate(self._peak_bps) if (self.connected and self._peak_bps > 0) else "—"

    @Property(str, notify=changed)
    def totalTraffic(self) -> str:
        if not self.connected:
            return "0 B"
        st = self._stats()
        return format_bytes(int(st.get("bytes_down", 0)) + int(st.get("bytes_up", 0)))

    @Property(str, notify=changed)
    def uptimeText(self) -> str:
        return format_uptime(int(self._stats().get("uptime", 0))) if self.connected else "Çevrimdışı"

    @Property(int, notify=changed)
    def dohHits(self) -> int:
        return int(self._stats().get("doh_hits", 0))

    @Property(int, notify=changed)
    def directCount(self) -> int:
        return int(self._stats().get("strategy_counts", {}).get("direct", 0))

    @Property(int, notify=changed)
    def tcpFragCount(self) -> int:
        return int(self._stats().get("strategy_counts", {}).get("tcp_frag", 0))

    @Property(int, notify=changed)
    def tlsRecordCount(self) -> int:
        return int(self._stats().get("strategy_counts", {}).get("tls_record", 0))

    @Property(int, notify=changed)
    def tunnelCount(self) -> int:
        return int(self._stats().get("strategy_counts", {}).get("tunnel", 0))

    @Property(int, notify=changed)
    def failedCount(self) -> int:
        return int(self._stats().get("strategy_counts", {}).get("failed", 0))

    @Property("QVariantList", notify=changed)
    def recentEvents(self) -> list:
        return list(self._stats().get("recent_events", []))

    # --- Diagnostics ---
    @Property(bool, notify=changed)
    def testing(self) -> bool:
        return self._testing

    @Property("QVariantList", notify=changed)
    def diagnosticResults(self) -> list:
        return list(self._diagnostic_results)

    # --- Async dispatch ---
    def _dispatch(self, kind: str, fn) -> None:
        if kind == "action":
            self._action_seq += 1
        seq = self._action_seq
        future = self._executor.submit(fn)

        def _done(res):
            if self._closing:
                return
            try:
                payload, err = res.result(), ""
            except Exception as exc:
                payload, err = None, str(exc) or "İşlem tamamlanamadı. Lütfen tekrar dene."
            self._actionCompleted.emit((kind, seq, payload), err)

        future.add_done_callback(_done)

    def _reapply_if_connected(self, *, registry_only: bool = False) -> None:
        if not self.connected or self._busy:
            self.changed.emit()
            return
        self._disconnecting = False
        self._busy = True
        self._error = ""
        self.changed.emit()
        self._dispatch("action", lambda: self.service.apply_config_live(registry_only=registry_only))

    def connectIfDisconnected(self) -> None:
        """Silently connect on --minimized startup if not already active."""
        if self._busy or self.connected:
            return
        self._disconnecting = False
        self._busy = True
        self._error = ""
        self.changed.emit()
        self._dispatch("action", self.service.enable)

    # --- Commands ---
    @Slot()
    def toggle(self) -> None:
        if self._busy:
            return
        self._disconnecting = self.connected
        disconnect = self._disconnecting
        self._busy = True
        self._error = ""
        self.changed.emit()
        self._dispatch("action", self.service.disable if disconnect else self.service.enable)

    @Slot(str)
    def setMode(self, mode: str) -> None:
        try:
            parsed = NetworkMode(mode)
        except ValueError:
            return
        if parsed == self.service.config.mode:
            return
        self.service.update_config(mode=parsed)
        self._reapply_if_connected()

    @Slot(bool)
    def setDohEnabled(self, enabled: bool) -> None:
        if bool(enabled) == self.service.config.doh_enabled:
            return
        self.service.update_config(doh_enabled=bool(enabled))
        self._reapply_if_connected()

    @Slot(bool)
    def setSystemProxy(self, enabled: bool) -> None:
        if bool(enabled) == self.service.config.system_proxy:
            return
        self.service.update_config(system_proxy=bool(enabled))
        self._reapply_if_connected(registry_only=True)

    @Slot(bool)
    def setAnimations(self, enabled: bool) -> None:
        self.service.update_config(animations=bool(enabled))
        self.changed.emit()

    @Slot(bool)
    def setStartWithWindows(self, enabled: bool) -> None:
        want = bool(enabled)
        try:
            actual = set_startup_enabled(want)
            self._start_with_windows = actual
            self.service.update_config(start_with_windows=actual)
            self._error = ""
        except Exception as exc:
            self._error = f"Başlangıç kısayolu ayarlanamadı: {exc}"
        self.changed.emit()

    @Slot()
    def flushCaches(self) -> None:
        if not self.connected or self._busy:
            return
        self._dispatch("flush", lambda: self.service.flush_caches(clear_persisted=True))

    @Slot(str)
    def addBypassDomain(self, raw: str) -> None:
        domain = sanitize_domain(raw)
        if not domain or domain in self.service.config.bypass_domains:
            return
        self.service.update_config(bypass_domains=(*self.service.config.bypass_domains, domain))
        self._reapply_if_connected()

    @Slot(str)
    def removeBypassDomain(self, domain: str) -> None:
        updated = tuple(d for d in self.service.config.bypass_domains if d != domain)
        self.service.update_config(bypass_domains=updated)
        self._reapply_if_connected()

    @Slot(str)
    def addForceDomain(self, raw: str) -> None:
        domain = sanitize_domain(raw)
        if not domain or domain in self.service.config.force_fragment_domains:
            return
        self.service.update_config(
            force_fragment_domains=(*self.service.config.force_fragment_domains, domain)
        )
        self._reapply_if_connected()

    @Slot(str)
    def removeForceDomain(self, domain: str) -> None:
        updated = tuple(d for d in self.service.config.force_fragment_domains if d != domain)
        self.service.update_config(force_fragment_domains=updated)
        self._reapply_if_connected()

    @Slot(bool)
    def setGameMode(self, enabled: bool) -> None:
        if self._busy:
            return
        want = bool(enabled)
        if want == self.gameModeActive:
            return
        self._busy = True
        self._error = ""
        self.changed.emit()
        self._dispatch("action", self.service.enable_game_mode if want else self.service.disable_game_mode)

    @Slot(str)
    def addGameApp(self, raw: str) -> None:
        app_name = sanitize_app_name(raw)
        if not app_name or any(a.lower() == app_name.lower() for a in self.service.config.game_mode_apps):
            return
        self.service.update_config(game_mode_apps=(*self.service.config.game_mode_apps, app_name))
        if self.gameModeActive and not self._busy:
            self._busy = True
            self.changed.emit()
            self._dispatch("action", self.service.enable_game_mode)
        else:
            self.changed.emit()

    @Slot()
    def browseGameApp(self) -> None:
        """Open native Windows file picker to select any .exe for Game Mode split-tunnel."""
        path, _ = QFileDialog.getOpenFileName(
            None,
            "Tünel Uygulaması Seç",
            os.environ.get("ProgramFiles", "C:\\"),
            "Uygulamalar (*.exe);;Tüm Dosyalar (*.*)",
        )
        if path:
            self.addGameApp(path)

    @Slot(str)
    def removeGameApp(self, app_name: str) -> None:
        updated = tuple(a for a in self.service.config.game_mode_apps if a.lower() != app_name.lower())
        self.service.update_config(game_mode_apps=updated)
        if self.gameModeActive and not self._busy:
            self._busy = True
            self.changed.emit()
            self._dispatch("action", self.service.enable_game_mode)
        else:
            self.changed.emit()

    @Slot()
    def runDiagnostics(self) -> None:
        if self._testing:
            return
        self._testing = True
        self.changed.emit()
        future = self._executor.submit(self.service.run_diagnostics)

        def _done(res):
            if self._closing:
                return
            try:
                results = res.result()
            except Exception:
                results = []
            self._diagnosticsCompleted.emit(results)

        future.add_done_callback(_done)

    @Slot()
    def openDataFolder(self) -> None:
        self.service.data_dir.mkdir(parents=True, exist_ok=True)
        try:
            os.startfile(str(self.service.data_dir))
        except OSError:
            pass

    @Slot()
    def poll(self) -> None:
        if not self._busy and not self._polling:
            self._polling = True
            if self._recovery_attempts < 2:
                self._dispatch("status", self.service.recover_if_needed)
            else:
                self._dispatch("status", self.service.status)

    @Slot(object, str)
    def _on_action_done(self, result, error: str) -> None:
        kind, seq, status = result
        if kind == "status":
            self._polling = False
            if self._busy or seq != self._action_seq:
                return
        elif kind == "flush":
            if error:
                self._error = error
            self.changed.emit()
            return
        else:
            self._busy = False
        if error:
            self._error = error
            if kind == "status":
                self._recovery_attempts += 1
        elif isinstance(status, ServiceStatus):
            self._status = status
            if status.connected and status.stats:
                self._recovery_attempts = 0
                bps = float(status.stats.get("bps_down", 0.0))
                if bps > self._peak_bps:
                    self._peak_bps = bps
            else:
                self._peak_bps = 0.0
            if kind == "action":
                self._error = ""
        self.changed.emit()

    @Slot(object)
    def _on_diagnostics_done(self, results) -> None:
        self._testing = False
        self._diagnostic_results = results or []
        self.changed.emit()

    def close(self) -> None:
        self._closing = True
        self.timer.stop()
        try:
            self.service.disable_game_mode()
        except Exception:
            pass
        self._executor.shutdown(wait=False, cancel_futures=True)
