#!/usr/bin/env python3
"""Capture a real Mainsail Web UI GIF driven by real Klipper (klippy).

Starts ``scripts/capture_sim_gifs.py --serve-mainsail`` (via WSL2 on Windows,
or directly on Linux) serving the official Mainsail static release over a
local Moonraker JSON-RPC HTTP/WebSocket bridge backed by real Klipper v0.14.0,
then drives a real headless Chromium/Chrome browser with Playwright to capture
``assets/mainsail-webui.gif``.
"""

from __future__ import annotations

import io
import os
import platform
import subprocess
import sys
import time
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

import requests
from PIL import Image
from playwright.sync_api import sync_playwright

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "assets"
CAPTURE_SCRIPT = REPO_ROOT / "scripts" / "capture_sim_gifs.py"

CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    "/usr/bin/google-chrome",
    "/usr/bin/chromium",
    "/usr/bin/chromium-browser",
)


def _find_browser() -> Optional[str]:
    env_bin = os.environ.get("BROWSER_PATH")
    if env_bin and os.path.isfile(env_bin):
        return env_bin
    for cand in CHROME_CANDIDATES:
        if os.path.isfile(cand):
            return cand
    return None


def _win_to_wsl(path_str: str) -> str:
    if len(path_str) >= 2 and path_str[1] == ":":
        drive = path_str[0].lower()
        rest = path_str[2:].replace("\\", "/")
        return f"/mnt/{drive}{rest}"
    return path_str.replace("\\", "/")


def _save_optimized_gif(
    path: Path,
    frames: Sequence[Image.Image],
    durations: Sequence[int],
    colors: int = 128,
) -> Tuple[int, int, int, int]:
    path.parent.mkdir(parents=True, exist_ok=True)
    sample_indices = sorted(
        set(range(0, len(frames), max(1, len(frames) // 8))) | {len(frames) - 1}
    )
    w, h = frames[0].size
    montage = Image.new("RGB", (w, h * len(sample_indices)))
    for i, idx in enumerate(sample_indices):
        montage.paste(frames[idx], (0, i * h))
    palette_img = montage.quantize(
        colors=colors, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE
    )
    quantized = [f.quantize(palette=palette_img, dither=Image.Dither.NONE) for f in frames]
    quantized[0].save(
        path,
        save_all=True,
        append_images=quantized[1:],
        duration=list(durations),
        loop=0,
        optimize=True,
    )
    return w, h, len(frames), path.stat().st_size


def capture_mainsail_gif(
    out_path: Path,
    mainsail_dir: str,
    port: int = 7126,
    viewport: Tuple[int, int] = (1024, 640),
    output_size: Tuple[int, int] = (800, 500),
) -> Tuple[int, int, int, int]:
    klipper_dir = os.environ.get("KLIPPER_DIR", "/tmp/klipper-sim")
    py_path = os.environ.get("WSL_PYTHONPATH", "/tmp/klippy-site")

    if platform.system() == "Windows":
        wsl_ms_dir = _win_to_wsl(mainsail_dir)
        cmd = [
            "wsl",
            "--",
            "env",
            f"PYTHONPATH={py_path}",
            f"KLIPPER_DIR={klipper_dir}",
            "python3",
            _win_to_wsl(str(CAPTURE_SCRIPT.resolve())),
            "--serve-mainsail",
            "--mainsail-dir",
            wsl_ms_dir,
            "--port",
            str(port),
        ]
    else:
        cmd = [
            sys.executable,
            str(CAPTURE_SCRIPT),
            "--serve-mainsail",
            "--mainsail-dir",
            mainsail_dir,
            "--port",
            str(port),
        ]

    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    base_url = f"http://127.0.0.1:{port}"
    try:
        # Wait for bridge ready line
        deadline = time.monotonic() + 20.0
        ready = False
        while time.monotonic() < deadline:
            line = proc.stdout.readline() if proc.stdout else ""
            if "MAINSAIL_BRIDGE_READY" in line:
                ready = True
                break
            if proc.poll() is not None:
                raise RuntimeError(f"Mainsail bridge exited early: {line}")
        if not ready:
            raise RuntimeError("Timed out waiting for Mainsail bridge")

        def sim_post(payload: dict) -> dict:
            r = requests.post(f"{base_url}/__sim", json=payload, timeout=10)
            r.raise_for_status()
            return r.json()

        frames: List[Image.Image] = []
        durations: List[int] = []

        browser_bin = _find_browser()
        with sync_playwright() as p:
            launch_kwargs = {"headless": True}
            if browser_bin:
                launch_kwargs["executable_path"] = browser_bin
            browser = p.chromium.launch(**launch_kwargs)
            page = browser.new_page(viewport={"width": viewport[0], "height": viewport[1]})

            def snap(ms: int) -> None:
                raw = page.screenshot(type="png")
                im = Image.open(io.BytesIO(raw)).convert("RGB")
                if im.size != output_size:
                    im = im.resize(output_size, Image.Resampling.LANCZOS)
                frames.append(im)
                durations.append(ms)

            page.goto(base_url, wait_until="networkidle", timeout=30000)
            # Wait for Mainsail dashboard cards to render
            page.wait_for_selector("text=Toolhead", timeout=20000)
            # Ensure Vuetify prompt dialogs have enough horizontal room for 3-4 footer buttons without clipping
            page.add_style_tag(
                content=(
                    ".v-dialog { max-width: 560px !important; } "
                    ".v-dialog .v-card__actions { flex-wrap: wrap !important; row-gap: 4px !important; }"
                )
            )
            time.sleep(0.8)

            # 1. Idle Mainsail dashboard
            sim_post({"action": "set_temps", "ext_c": 25.0, "ext_t": 0.0, "bed_c": 24.0, "bed_t": 0.0, "m117": "Ready"})
            time.sleep(0.5)
            snap(1500)

            # 2. Filament inserted -> real Mainsail "PrusaKLoad: Select Filament" dialog!
            sim_post({"action": "sensor", "present": True})
            page.wait_for_selector("text=PrusaKLoad: Select Filament", timeout=10000)
            time.sleep(0.5)
            snap(1700)

            # Hover PLA button and capture, then click it
            pla_btn = page.locator(".v-dialog button", has_text="PLA").first
            pla_btn.hover()
            time.sleep(0.25)
            snap(450)

            # 3. Dismiss picker cleanly & show real Mainsail heating/loading progress
            sim_post({"action": "close_prompt"})
            time.sleep(0.3)
            for ext_c, m117 in ((125.0, "Heat PLA 215C"), (215.0, "Loading PLA")):
                sim_post({"action": "set_temps", "ext_c": ext_c, "ext_t": 215.0, "m117": m117})
                time.sleep(0.5)
                snap(750)

            # 4. Real PRUSA_KLOAD_LOAD MATERIAL=PLA -> real Mainsail "PrusaKLoad: Purge Check (PLA)" dialog!
            sim_post({"action": "gcode", "script": "PRUSA_KLOAD_LOAD MATERIAL=PLA"})
            page.wait_for_selector("text=PrusaKLoad: Purge Check (PLA)", timeout=10000)
            time.sleep(0.5)
            snap(1800)

            yes_btn = page.locator(".v-dialog button", has_text="Yes (Color Clear)").first
            yes_btn.hover()
            time.sleep(0.25)
            snap(450)
            yes_btn.click()
            time.sleep(0.6)

            # 5. Start PETG print while PLA is loaded -> real Mainsail "PrusaKLoad: Material Mismatch!" dialog!
            sim_post({"action": "set_temps", "ext_c": 26.0, "ext_t": 0.0, "m117": "Loaded PLA"})
            sim_post({"action": "gcode", "script": "SDCARD_PRINT_FILE FILENAME=petg_bracket.gcode"})
            page.wait_for_selector("text=PrusaKLoad: Material Mismatch!", timeout=10000)
            time.sleep(0.5)
            snap(2100)

            swap_btn = page.locator(".v-dialog button", has_text="Swap Filament").first
            swap_btn.hover()
            time.sleep(0.25)
            snap(550)

            browser.close()

        try:
            sim_post({"action": "shutdown"})
        except Exception:
            pass
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    return _save_optimized_gif(out_path, frames, durations, colors=128)


def main() -> int:
    mainsail_dir = os.environ.get("MAINSAIL_DIR", "/tmp/mainsail")
    if len(sys.argv) > 1:
        mainsail_dir = sys.argv[1]
    out_path = ASSETS_DIR / "mainsail-webui.gif"
    w, h, n, sz = capture_mainsail_gif(out_path, mainsail_dir=mainsail_dir)
    print(f"Captured real Mainsail Web UI -> assets/mainsail-webui.gif: {w}x{h}, {n} frames, {sz/1024:.1f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
