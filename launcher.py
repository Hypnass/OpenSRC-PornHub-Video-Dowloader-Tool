"""Bootstrap launcher for PHDownloader.

Run this to start the app. When launched from source it checks that the required
Python packages are installed and, if any are missing, installs them with pip
before starting the GUI. ffmpeg is fetched automatically by the GUI on first run.

    python launcher.py

When frozen into an executable (PyInstaller) the dependency step is skipped
because everything is already bundled.
"""

from __future__ import annotations

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent

# import name -> pip requirement spec
REQUIRED = {
    "PyQt6": "PyQt6==6.8.1",
    "PyQt6.QtSvg": "PyQt6==6.8.1",
    "yt_dlp": "yt-dlp>=2024.4.9",
    "aiohttp": "aiohttp>=3.9.0",
    "requests": "requests>=2.31.0",
    "tqdm": "tqdm>=4.66.0",
}


def _missing_specs() -> list[str]:
    specs: list[str] = []
    for module, spec in REQUIRED.items():
        try:
            importlib.import_module(module)
        except Exception:  # noqa: BLE001 - any import failure means "install it"
            if spec not in specs:
                specs.append(spec)
    return specs


def _install(specs: list[str]) -> None:
    print("[setup] Installing missing dependencies: " + ", ".join(specs))
    subprocess.check_call(
        [sys.executable, "-m", "pip", "install", "--upgrade", *specs]
    )
    print("[setup] Dependencies installed.")


def main() -> int:
    if not getattr(sys, "frozen", False):
        missing = _missing_specs()
        if missing:
            try:
                _install(missing)
            except Exception as exc:  # noqa: BLE001
                print(f"[setup] Automatic install failed: {exc}")
                print(f"[setup] Please run:  pip install -r {ROOT / 'gui' / 'requirements.txt'}")
                return 1

    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))

    from gui.main import main as gui_main  # imported after deps are ensured

    return gui_main()


if __name__ == "__main__":
    sys.exit(main())
