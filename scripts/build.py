"""Build the standalone Windows Relay.exe from the clean src/relay layout."""
from __future__ import annotations

import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
sys.path.insert(0, str(SRC))

from relay.ui.assets import build_brand_assets  # noqa: E402


def main() -> None:
    ui_dir = SRC / "relay" / "ui"
    qml_dir = ui_dir / "qml"
    assets_dir = ui_dir / "assets"
    icon_path = build_brand_assets(assets_dir)

    command = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--onefile",
        "--windowed",
        "--name",
        "Relay",
        "--icon",
        str(icon_path),
        "--collect-all",
        "PySide6.QtQml",
        "--add-data",
        f"{qml_dir}{os.pathsep}relay_ui/qml",
        "--add-data",
        f"{assets_dir}{os.pathsep}relay_ui/assets",
        "--paths",
        str(SRC),
        "--distpath",
        str(ROOT / "dist"),
        "--workpath",
        str(ROOT / "build" / "pyinstaller"),
        "--specpath",
        str(ROOT / "build" / "pyinstaller"),
    ]
    if "--reuse-cache" not in sys.argv[1:]:
        command.append("--clean")

    qt_dir = Path(importlib.util.find_spec("PySide6").origin).parent
    for dll in ("VCRUNTIME140.dll", "VCRUNTIME140_1.dll", "MSVCP140.dll", "MSVCP140_1.dll", "MSVCP140_2.dll"):
        if (qt_dir / dll).is_file():
            command.extend(["--add-binary", f"{qt_dir / dll}{os.pathsep}."])

    usque_bin = ROOT / "bin" / "usque.exe"
    if usque_bin.is_file():
        command.extend(["--add-binary", f"{usque_bin}{os.pathsep}relay_bin"])

    for excluded in (
        "numpy",
        "torch",
        "cv2",
        "matplotlib",
        "scipy",
        "tkinter",
        "customtkinter",
        "PySide6.QtWebEngineCore",
        "PySide6.QtWebEngineWidgets",
        "PySide6.QtWebEngineQuick",
        "PySide6.QtPdf",
        "PySide6.QtPdfWidgets",
    ):
        command.extend(["--exclude-module", excluded])

    command.append(str(SRC / "relay" / "__main__.py"))

    env = dict(os.environ)
    env["PATH"] = os.pathsep.join(
        [
            str(Path(sys.executable).parent),
            str(Path(sys.base_prefix)),
            str(qt_dir),
            str(Path(os.environ["SystemRoot"]) / "System32"),
            os.environ["SystemRoot"],
        ]
    )

    subprocess.run(command, cwd=ROOT, env=env, check=True)
    built = ROOT / "dist" / "Relay.exe"
    if "--dist-only" not in sys.argv[1:]:
        desktop_target = Path.home() / "Desktop" / "Relay.exe"
        shutil.copy2(built, desktop_target)
        print(f"Built: {desktop_target}")
    else:
        print(f"Built: {built}")


if __name__ == "__main__":
    main()
