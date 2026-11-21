#!/usr/bin/env python3
"""Entry point to regenerate PrusaKLoad's real simulator UI preview GIFs.

Delegates to ``scripts/capture_sim_gifs.py`` (running directly on Linux, or
automatically via WSL2 when invoked on Windows).

Generates:
  - ``assets/klipperscreen-load.gif`` (real KlipperScreen GTK3 + real Klipper)
  - ``assets/klipperscreen-mismatch.gif`` (real KlipperScreen GTK3 + real Klipper)
  - ``assets/printer-display-lcd.gif`` (real Klipper UC1701 128x64 VRAM + MenuManager)
"""

from __future__ import annotations

import os
import platform
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CAPTURE_SCRIPT = REPO_ROOT / "scripts" / "capture_sim_gifs.py"


def _win_to_wsl(path: Path) -> str:
    resolved = str(path.resolve())
    if len(resolved) >= 2 and resolved[1] == ":":
        drive = resolved[0].lower()
        rest = resolved[2:].replace("\\", "/")
        return f"/mnt/{drive}{rest}"
    return resolved.replace("\\", "/")


def main() -> int:
    extra_args = sys.argv[1:]
    if platform.system() != "Windows":
        cmd = [sys.executable, str(CAPTURE_SCRIPT), *extra_args]
        return subprocess.call(cmd)

    klipper_dir = os.environ.get("KLIPPER_DIR", "/tmp/klipper-sim")
    ks_dir = os.environ.get("KLIPPERSCREEN_DIR", "/tmp/KlipperScreen")
    py_path = os.environ.get("WSL_PYTHONPATH", "/tmp/klippy-site")
    cmd = [
        "wsl",
        "--",
        "env",
        "GDK_BACKEND=x11",
        f"PYTHONPATH={py_path}",
        f"KLIPPER_DIR={klipper_dir}",
        f"KLIPPERSCREEN_DIR={ks_dir}",
        "python3",
        _win_to_wsl(CAPTURE_SCRIPT),
        *extra_args,
    ]
    return subprocess.call(cmd)


if __name__ == "__main__":
    sys.exit(main())
