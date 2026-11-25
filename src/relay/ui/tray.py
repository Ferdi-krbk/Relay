"""System tray controller and low-memory QML window teardown manager."""
from __future__ import annotations

import ctypes
import gc
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QUrl, Qt, Slot
from PySide6.QtGui import QAction, QBrush, QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickWindow
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon

from relay.ui.viewmodel import RelayViewModel


def _build_badged_icon(base: QIcon, badge_hex: str | None) -> QIcon:
    """Return base icon or a crisp HiDPI status-badged variant for the system tray."""
    if not badge_hex:
        return base
    out = QIcon()
    for sz in (16, 24, 32, 48, 64):
        pm = base.pixmap(sz, sz)
        if pm.isNull():
            continue
        canvas = QPixmap(pm)
        painter = QPainter(canvas)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        dot_d = max(5, int(round(sz * 0.36)))
        ring_w = max(1, int(round(sz * 0.08)))
        x = sz - dot_d - max(1, ring_w // 2)
        y = sz - dot_d - max(1, ring_w // 2)
        painter.setPen(QPen(QColor("#161412"), ring_w))
        painter.setBrush(QBrush(QColor(badge_hex)))
        painter.drawEllipse(x, y, dot_d, dot_d)
        painter.end()
        out.addPixmap(canvas)
    return out if not out.isNull() else base


class TrayManager(QObject):
    """Owns the system tray icon and lazily creates/destroys the Qt Quick window."""

    def __init__(
        self,
        app: QApplication,
        viewmodel: RelayViewModel,
        qml_dir: Path,
        assets_dir: Path,
        icon: QIcon,
    ):
        super().__init__(app)
        self.app = app
        self.viewmodel = viewmodel
        self.qml_dir = qml_dir
        self.assets_dir = assets_dir
        self.icon = icon
        self._icon_idle = icon
        self._icon_connected = _build_badged_icon(icon, "#22A06B")
        self._icon_error = _build_badged_icon(icon, "#E54826")
        self._has_shown_intro = False
        self.window: QQuickWindow | None = None
        self.engine: QQmlApplicationEngine | None = None
        self.position = None

        self._unload_timer = QTimer(self)
        self._unload_timer.setSingleShot(True)
        self._unload_timer.timeout.connect(self.unload_window)

        self._compact_timer = QTimer(self)
        self._compact_timer.setSingleShot(True)
        self._compact_timer.timeout.connect(self.compact_memory)

        self.tray = QSystemTrayIcon(icon, self)
        self.menu = QMenu()

        open_action = QAction("Relay'i Aç", self.menu)
        open_action.triggered.connect(self.showWindow)
        self.menu.addAction(open_action)

        self._toggle_action = QAction("Bağlan", self.menu)
        self._toggle_action.triggered.connect(self.viewmodel.toggle)
        self.menu.addAction(self._toggle_action)

        self.menu.addSeparator()

        quit_keep = QAction("Çıkış (Bağlantıyı Koru)", self.menu)
        quit_keep.triggered.connect(app.quit)
        self.menu.addAction(quit_keep)

        quit_stop = QAction("Bağlantıyı Kes ve Çık", self.menu)
        quit_stop.triggered.connect(self._disconnect_and_quit)
        self.menu.addAction(quit_stop)

        def _sync_menu() -> None:
            busy = viewmodel.busy
            self._toggle_action.setEnabled(not busy)
            self._toggle_action.setText("Bağlantıyı Kes" if viewmodel.connected else "Bağlan")
            quit_keep.setEnabled(not busy)
            quit_stop.setEnabled(not busy)
            if viewmodel.error:
                self.tray.setIcon(self._icon_error)
                state_txt = "Hata"
            elif viewmodel.connected:
                self.tray.setIcon(self._icon_connected)
                state_txt = "Bağlı"
            else:
                self.tray.setIcon(self._icon_idle)
                state_txt = "Hazır"
            self.tray.setToolTip(f"Relay ({state_txt}) — açmak için tıkla")

        viewmodel.changed.connect(_sync_menu)
        _sync_menu()

        self.tray.setContextMenu(self.menu)
        self.tray.activated.connect(self._on_activated)
        self.tray.show()
        app.aboutToQuit.connect(self.tray.hide)

    def _disconnect_and_quit(self) -> None:
        try:
            self.viewmodel.service.disable()
        except Exception:
            pass
        self.app.quit()

    def create_window(self) -> None:
        self.engine = QQmlApplicationEngine(self)
        self.engine.rootContext().setContextProperty("network", self.viewmodel)
        self.engine.rootContext().setContextProperty("desktop", self)
        self.engine.rootContext().setContextProperty("showIntro", not self._has_shown_intro)
        self.engine.rootContext().setContextProperty(
            "iconSource", QUrl.fromLocalFile(str(self.assets_dir / "relay-icon.png")).toString()
        )
        self.engine.load(QUrl.fromLocalFile(str(self.qml_dir / "Main.qml")))
        if not self.engine.rootObjects():
            raise RuntimeError("Relay arayüzü yüklenemedi.")
        root = self.engine.rootObjects()[0]
        if not isinstance(root, QQuickWindow):
            raise RuntimeError("Qt Quick penceresi oluşturulamadı.")
        self.window = root
        self.window.setPersistentGraphics(False)
        self.window.setPersistentSceneGraph(False)
        self.window.setIcon(self.icon)
        if self.position is not None:
            self.window.setPosition(self.position)
        self._has_shown_intro = True

    @Slot()
    def unload_window(self) -> None:
        if self.window is None or self.window.isVisible():
            return
        self.position = self.window.position()
        self.window.releaseResources()
        self.window.deleteLater()
        if self.engine is not None:
            self.engine.deleteLater()
        self.window = None
        self.engine = None
        self._compact_timer.start(600)

    @Slot()
    def compact_memory(self) -> None:
        if self.window is not None:
            return
        gc.collect()
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.GetCurrentProcess.restype = ctypes.c_void_p
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        psapi.EmptyWorkingSet.argtypes = [ctypes.c_void_p]
        psapi.EmptyWorkingSet.restype = ctypes.c_int
        psapi.EmptyWorkingSet(kernel.GetCurrentProcess())

    @Slot()
    def hideWindow(self) -> None:
        if self.window is None:
            return
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.window.hide()
            self.viewmodel.timer.setInterval(30000)
            self._unload_timer.start(250)
        else:
            self.window.showMinimized()

    @Slot()
    def showWindow(self) -> None:
        self._unload_timer.stop()
        self._compact_timer.stop()
        created = self.window is None
        if created:
            self.create_window()
        assert self.window is not None
        self.viewmodel.timer.setInterval(1000)
        self.viewmodel.poll()
        self.window.showNormal()
        self.window.raise_()
        self.window.requestActivate()
        if not created:
            try:
                self.window.playOpen()
            except Exception:
                pass

    def _on_activated(self, reason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.showWindow()
