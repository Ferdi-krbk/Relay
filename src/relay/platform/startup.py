"""Kaspersky-safe Windows Startup folder (.lnk) management (never touches Registry Run/RunOnce)."""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys


def startup_shortcut_path() -> Path:
    appdata = os.environ.get("APPDATA", "")
    if appdata:
        return Path(appdata) / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Relay.lnk"
    return Path.home() / "AppData" / "Roaming" / "Microsoft" / "Windows" / "Start Menu" / "Programs" / "Startup" / "Relay.lnk"


def is_startup_enabled() -> bool:
    return startup_shortcut_path().is_file()


def set_startup_enabled(enabled: bool) -> bool:
    """Create or remove a standard Startup folder .lnk shortcut with --minimized."""
    lnk = startup_shortcut_path()
    if not enabled:
        try:
            lnk.unlink(missing_ok=True)
        except OSError:
            pass
        return False

    if getattr(sys, "frozen", False):
        target = sys.executable
        arguments = "--minimized"
        workdir = str(Path(sys.executable).parent)
    else:
        target = sys.executable
        arguments = "-m relay --minimized"
        workdir = str(Path(__file__).resolve().parents[3])

    def _ps_escape(val: str) -> str:
        return val.replace("'", "''")

    script = (
        "$ws = New-Object -ComObject WScript.Shell; "
        f"$sc = $ws.CreateShortcut('{_ps_escape(str(lnk))}'); "
        f"$sc.TargetPath = '{_ps_escape(target)}'; "
        f"$sc.Arguments = '{_ps_escape(arguments)}'; "
        f"$sc.WorkingDirectory = '{_ps_escape(workdir)}'; "
        "$sc.Description = 'Relay DPI & DoH Proxy'; "
        "$sc.Save()"
    )
    lnk.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["powershell.exe", "-NoProfile", "-WindowStyle", "Hidden", "-Command", script],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        timeout=8,
        check=True,
    )
    return lnk.is_file()
