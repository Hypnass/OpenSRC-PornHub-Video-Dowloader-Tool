"""Build a standalone PHDownloader executable with PyInstaller.

Usage:
    python build_exe.py            # single-file .exe (default, easy to share)
    python build_exe.py --onedir   # one-folder build (faster start)

PyInstaller is installed automatically if it is missing. The resulting program
is written to ``dist/PHDownloader.exe`` (one-file) or ``dist/PHDownloader/``
(one-folder). ffmpeg is NOT bundled; the app downloads it on first run.
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent


def _ensure_pyinstaller() -> None:
    try:
        importlib.import_module("PyInstaller")
    except Exception:
        print("[build] Installing PyInstaller…")
        subprocess.check_call([sys.executable, "-m", "pip", "install", "pyinstaller"])


def main() -> int:
    _ensure_pyinstaller()
    # One-file by default; pass --onedir for the faster-starting folder build.
    onefile = "--onedir" not in sys.argv
    sep = ";" if os.name == "nt" else ":"

    args = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--name", "PHDownloader",
        "--windowed",
        "--onefile" if onefile else "--onedir",
        f"--add-data=gui/resources{sep}gui/resources",
        "--collect-all", "yt_dlp",
        "--hidden-import", "PyQt6.QtSvg",
        "--hidden-import", "PyQt6.QtSvgWidgets",
        # Backend modules live at the project root next to this script.
        "--paths", str(ROOT),
        "--hidden-import", "config",
        "--hidden-import", "downloader",
        "--hidden-import", "extractor",
        "--hidden-import", "utils",
        "launcher.py",
    ]
    print("[build] Running:", " ".join(args))
    subprocess.check_call(args, cwd=str(ROOT))

    out = ROOT / "dist" / ("PHDownloader.exe" if onefile else "PHDownloader")
    print(f"\n[build] Done. Output: {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
