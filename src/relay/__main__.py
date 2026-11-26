"""CLI and executable entry point for Relay.

Modes:
  - default:            Launch the Qt Quick desktop application & tray icon
  - --proxy-worker:     Run the headless async proxy engine subprocess
  - --restore-proxy:    Silently restore saved Windows proxy settings (RunOnce safety net)
"""
from __future__ import annotations

import sys


def _run_restore() -> None:
    from relay.service.controller import create_default_service

    try:
        create_default_service().disable(stop_worker=False)
    except Exception:
        pass


class _BoundedLogStream:
    """Caps long-running worker stdout/stderr growth without external dependencies."""

    def __init__(self, wrapped, max_bytes: int = 1_048_576):
        self._wrapped = wrapped
        self._max_bytes = max_bytes

    def write(self, data: str) -> int:
        if not data:
            return 0
        try:
            pos = self._wrapped.tell()
            if pos > self._max_bytes:
                self._wrapped.seek(0)
                self._wrapped.truncate(0)
        except OSError:
            pass
        return self._wrapped.write(data)

    def flush(self) -> None:
        self._wrapped.flush()

    def __getattr__(self, name: str):
        return getattr(self._wrapped, name)


def _run_worker(argv: list[str]) -> None:
    import argparse
    import asyncio
    import ctypes

    from relay.core.config import NetworkMode
    from relay.proxy.server import run_proxy_server

    if sys.stdout is not None:
        sys.stdout = _BoundedLogStream(sys.stdout)  # type: ignore[assignment]
    if sys.stderr is not None:
        sys.stderr = _BoundedLogStream(sys.stderr)  # type: ignore[assignment]

    # Ensure Windows shutdown/logoff cleans up proxy settings even if UI was exited ("keep connection").
    handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

    def _on_ctrl(ctrl_type: int) -> bool:
        if ctrl_type in (5, 6):  # CTRL_LOGOFF_EVENT, CTRL_SHUTDOWN_EVENT
            _run_restore()
        return False

    handler_ref = handler_type(_on_ctrl)
    try:
        ctypes.windll.kernel32.SetConsoleCtrlHandler(handler_ref, True)
    except Exception:
        pass

    parser = argparse.ArgumentParser(prog="relay --proxy-worker")
    parser.add_argument("--proxy-worker", action="store_true")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--mode", choices=[m.value for m in NetworkMode], default=NetworkMode.AUTO.value)
    parser.add_argument("--delay", type=float, default=None)
    parser.add_argument("--race-delay", type=float, default=None)
    parser.add_argument("--tls-records", action="store_true")
    parser.add_argument("--no-doh", action="store_true")
    parser.add_argument("--no-tunnel", action="store_true")
    parser.add_argument("--bypass", type=str, default="")
    parser.add_argument("--force-fragment", type=str, default="")
    args = parser.parse_args(argv)

    try:
        asyncio.run(run_proxy_server(args))
    except KeyboardInterrupt:
        pass


def _run_game_tunnel(argv: list[str]) -> None:
    import argparse

    from relay.core.config import GAME_CONTROL_PORT
    from relay.proxy.gametunnel import run_game_tunnel_supervisor

    parser = argparse.ArgumentParser(prog="relay --game-tunnel")
    parser.add_argument("--game-tunnel", action="store_true")
    parser.add_argument("--config", type=str, required=True)
    parser.add_argument("--control-port", type=int, default=GAME_CONTROL_PORT)
    parser.add_argument("--parent-pid", type=int, default=0)
    parser.add_argument("--token", type=str, default="")
    args = parser.parse_args(argv)
    run_game_tunnel_supervisor(args)


def _run_ui(argv: list[str]) -> None:
    from relay.ui.app import run_desktop_app

    run_desktop_app(argv)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if "--proxy-worker" in args:
        _run_worker(args)
    elif "--game-tunnel" in args:
        _run_game_tunnel(args)
    elif "--restore-proxy" in args:
        _run_restore()
    else:
        _run_ui(args)


if __name__ == "__main__":
    main()
