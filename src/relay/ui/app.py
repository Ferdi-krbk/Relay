"""Desktop application bootstrapper (HiDPI, MSAA, single-instance IPC, shutdown hook)."""
from __future__ import annotations

import ctypes
import hashlib
import os
from pathlib import Path
import sys

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication, QIcon, QSurfaceFormat
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import QApplication

from relay.service.controller import create_default_service
from relay.ui.assets import build_brand_assets
from relay.ui.tray import TrayManager
from relay.ui.viewmodel import RelayViewModel


def _resolve_ui_dirs() -> tuple[Path, Path]:
    if hasattr(sys, "_MEIPASS"):
        base = Path(sys._MEIPASS) / "relay_ui"
        return base / "qml", base / "assets"
    here = Path(__file__).resolve().parent
    return here / "qml", here / "assets"


def run_desktop_app(argv: list[str] | None = None) -> None:
    os.environ.setdefault("QT_QUICK_CONTROLS_STYLE", "Basic")
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("Relay.Desktop.2")
    except Exception:
        pass

    QGuiApplication.setHighDpiScaleFactorRoundingPolicy(
        Qt.HighDpiScaleFactorRoundingPolicy.PassThrough
    )
    fmt = QSurfaceFormat()
    fmt.setSamples(4)
    QSurfaceFormat.setDefaultFormat(fmt)

    app = QApplication(sys.argv if argv is None else [sys.argv[0], *argv])
    app.setQuitOnLastWindowClosed(False)

    ipc_name = "RelayDesktop-" + hashlib.sha256(str(Path.home()).encode()).hexdigest()[:12]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_bool, ctypes.c_wchar_p]
    kernel.CreateMutexW.restype = ctypes.c_void_p
    kernel.CloseHandle.argtypes = [ctypes.c_void_p]
    mutex = kernel.CreateMutexW(None, False, "Local\\RelayDesktop")

    if ctypes.get_last_error() == 183:
        sock = QLocalSocket()
        sock.connectToServer(ipc_name)
        if sock.waitForConnected(1500):
            sock.disconnectFromServer()
            kernel.CloseHandle(mutex)
            return
        user32 = ctypes.WinDLL("user32")
        user32.FindWindowW.argtypes = [ctypes.c_wchar_p, ctypes.c_wchar_p]
        user32.FindWindowW.restype = ctypes.c_void_p
        user32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        user32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        hwnd = user32.FindWindowW(None, "Relay")
        if hwnd:
            user32.ShowWindow(hwnd, 9)
            user32.SetForegroundWindow(hwnd)
        kernel.CloseHandle(mutex)
        return

    app.setApplicationName("Relay")
    app.setOrganizationName("Relay")

    qml_dir, assets_dir = _resolve_ui_dirs()
    icon_file = assets_dir / "relay-icon.png"
    if not icon_file.is_file() and not hasattr(sys, "_MEIPASS"):
        build_brand_assets(assets_dir)
    if not icon_file.is_file():
        icon_file = assets_dir / "relay.ico"
    app.setWindowIcon(QIcon(str(icon_file)))

    service = create_default_service()
    viewmodel = RelayViewModel(service)
    tray = TrayManager(app, viewmodel, qml_dir, assets_dir, app.windowIcon())

    server = QLocalServer(app)
    server.setSocketOptions(QLocalServer.SocketOption.UserAccessOption)
    if not server.listen(ipc_name):
        QLocalServer.removeServer(ipc_name)
        if not server.listen(ipc_name):
            raise RuntimeError("Sistem tepsisi kanalı başlatılamadı.")

    def _activate() -> None:
        while server.hasPendingConnections():
            conn = server.nextPendingConnection()
            tray.showWindow()
            conn.disconnectFromServer()
            conn.deleteLater()

    server.newConnection.connect(_activate)
    minimized = "--minimized" in (argv if argv is not None else sys.argv[1:])
    if minimized:
        viewmodel.timer.setInterval(30000)
        viewmodel.connectIfDisconnected()
    else:
        tray.showWindow()

    def _on_session_ending(_manager) -> None:
        try:
            service.disable()
        except Exception:
            pass

    app.commitDataRequest.connect(_on_session_ending)
    app.aboutToQuit.connect(viewmodel.close)
    try:
        app.exec()
    finally:
        kernel.CloseHandle(mutex)
