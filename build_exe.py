"""Build SmoothCursor.exe with PyInstaller."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ENTRY = ROOT / "run.py"
DIST = ROOT / "dist"
BUILD = ROOT / "build"
SPEC = ROOT / "SmoothCursor.spec"


def main() -> int:
    try:
        import PyInstaller  # noqa: F401
    except ImportError:
        print("Installing PyInstaller...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller>=6.0"])

    cmd = [
        sys.executable,
        "-m",
        "PyInstaller",
        "--noconfirm",
        "--clean",
        "--windowed",
        "--onefile",
        "--name",
        "SmoothCursor",
        "--distpath",
        str(DIST),
        "--workpath",
        str(BUILD),
        "--specpath",
        str(ROOT),
        # pywin32 / win32com often need these
        "--hidden-import",
        "win32timezone",
        "--hidden-import",
        "pythoncom",
        "--hidden-import",
        "pywintypes",
        "--hidden-import",
        "win32api",
        "--hidden-import",
        "win32con",
        "--hidden-import",
        "win32gui",
        "--hidden-import",
        "win32process",
        "--hidden-import",
        "customtkinter",
        "--hidden-import",
        "pystray",
        "--hidden-import",
        "pystray._win32",
        "--collect-all",
        "customtkinter",
        "--collect-all",
        "pystray",
        str(ENTRY),
    ]
    print(" ".join(cmd))
    return subprocess.call(cmd)


if __name__ == "__main__":
    raise SystemExit(main())
