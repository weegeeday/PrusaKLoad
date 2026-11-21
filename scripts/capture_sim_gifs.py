#!/usr/bin/env python3
"""Capture authentic UI preview GIFs from real Klipper + real KlipperScreen.

Runs inside WSL2 (or Linux) against:
  1. Real upstream Klipper (klippy) with PrusaKLoad loaded:
     - Captures the actual 128x64 monochrome pixel framebuffer from Klipper's
       ``uc1701`` driver + ``MenuManager`` (``config/prusa_kload_menu.cfg``)
       -> ``assets/printer-display-lcd.gif``
  2. Real upstream KlipperScreen (GTK3) connected via a local Moonraker
     JSON-RPC WebSocket bridge backed by real Klipper (klippy):
     - Captures real GTK3 frames for the MK4S load flow
       -> ``assets/klipperscreen-load.gif``
     - Captures real GTK3 frames for the pre-print material mismatch & swap flow
       -> ``assets/klipperscreen-mismatch.gif``
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import mimetypes
import os
import queue
import socket
import struct
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from urllib.parse import urlparse

from PIL import Image, ImageDraw

REPO_ROOT = Path(__file__).resolve().parent.parent
ASSETS_DIR = REPO_ROOT / "assets"
MENU_CFG = REPO_ROOT / "config" / "prusa_kload_menu.cfg"
KLIPPER_DIR = Path(os.environ.get("KLIPPER_DIR", "/tmp/klipper-sim"))
KLIPPER_DICT = Path(os.environ.get("KLIPPER_DICT", str(KLIPPER_DIR / "out" / "klipper.dict")))
KS_DIR = Path(os.environ.get("KLIPPERSCREEN_DIR", "/tmp/KlipperScreen"))

# ---------------------------------------------------------------------------
# Shared GIF optimization helper
# ---------------------------------------------------------------------------


def save_optimized_gif(
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


# ---------------------------------------------------------------------------
# Klipper printer.cfg template for real in-process simulation
# ---------------------------------------------------------------------------

SIM_PRINTER_CFG = """
[stepper_x]
step_pin: 1
dir_pin: 2
enable_pin: !3
microsteps: 16
rotation_distance: 40
endstop_pin: ^4
position_endstop: 0
position_max: 200
homing_speed: 50

[stepper_y]
step_pin: 5
dir_pin: !6
enable_pin: !7
microsteps: 16
rotation_distance: 40
endstop_pin: ^8
position_endstop: 0
position_max: 200
homing_speed: 50

[stepper_z]
step_pin: 9
dir_pin: 10
enable_pin: !11
microsteps: 16
rotation_distance: 8
endstop_pin: ^12
position_endstop: 0.5
position_max: 200

[extruder]
step_pin: 13
dir_pin: 14
enable_pin: !15
microsteps: 16
rotation_distance: 33.5
nozzle_diameter: 0.400
filament_diameter: 1.750
heater_pin: 16
sensor_type: EPCOS 100K B57560G104F
sensor_pin: 17
control: pid
pid_Kp: 22.2
pid_Ki: 1.08
pid_Kd: 114
min_temp: 0
max_temp: 300
min_extrude_temp: 0
max_extrude_only_distance: 50.0

[heater_bed]
heater_pin: 18
sensor_type: EPCOS 100K B57560G104F
sensor_pin: 19
control: pid
pid_Kp: 54.0
pid_Ki: 0.77
pid_Kd: 948.0
min_temp: 0
max_temp: 130

[fan]
pin: 29

[mcu]
serial: /dev/ttyACM0

[printer]
kinematics: cartesian
max_velocity: 300
max_accel: 3000
max_z_velocity: 5
max_z_accel: 100

[virtual_sdcard]
path: {sdcard_dir}

[pause_resume]

[display_status]

[display]
lcd_type: uc1701
cs_pin: 20
a0_pin: 21
rst_pin: 22
spi_software_sclk_pin: 23
spi_software_mosi_pin: 24
spi_software_miso_pin: 25
encoder_pins: ^26, ^27
click_pin: ^!28

[include {menu_cfg}]

[filament_switch_sensor fsensor]
switch_pin: ^!30

[prusa_kload]
filament_sensor: filament_switch_sensor fsensor
state_file: {state_file}
debounce_delay: 0.05
load_distance: 50.0
purge_distance: 30.0
unload_distance: 80.0
ramming_distance: 10.0
cooling_moves: 1
prompt_timeout: 300.0
"""


def _prepare_sim_dict(tmpdir: str) -> str:
    with open(KLIPPER_DICT, "r", encoding="utf-8") as handle:
        dict_data = json.load(handle)
    dict_cfg = dict_data.setdefault("config", {})
    dict_cfg.setdefault("ADC_MAX", 1023)
    dict_cfg.setdefault("PWM_MAX", 255)
    sim_dict = os.path.join(tmpdir, "sim.dict")
    with open(sim_dict, "w", encoding="utf-8") as handle:
        json.dump(dict_data, handle)
    return sim_dict


def _ensure_klippy_on_path() -> None:
    klippy_dir = str(KLIPPER_DIR / "klippy")
    if klippy_dir not in sys.path:
        sys.path.insert(0, klippy_dir)


# ---------------------------------------------------------------------------
# PART 1: Real Klipper UC1701 128x64 Physical LCD Capture
# ---------------------------------------------------------------------------

LCD_BG = (10, 34, 74)
LCD_FG = (218, 240, 255)
LCD_BEZEL = (20, 24, 30)
LCD_FRAME = (46, 56, 70)


def _vram_to_image(vram: Sequence[bytearray], scale: int = 4) -> Image.Image:
    """Decode real Klipper UC1701 8x128 page VRAM into a crisp 128x64 image."""
    lcd = Image.new("RGB", (128, 64), LCD_BG)
    pixels = lcd.load()
    for page in range(8):
        row_bytes = vram[page]
        base_y = page * 8
        for x in range(128):
            b = row_bytes[x]
            if b:
                for bit in range(8):
                    if b & (1 << bit):
                        pixels[x, base_y + bit] = LCD_FG

    scaled = lcd.resize((128 * scale, 64 * scale), Image.Resampling.NEAREST)
    pad = 16
    w, h = 128 * scale + pad * 2, 64 * scale + pad * 2
    canvas = Image.new("RGB", (w, h), LCD_BEZEL)
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle(
        (4, 4, w - 5, h - 5),
        radius=10,
        fill=(26, 31, 39),
        outline=LCD_FRAME,
        width=2,
    )
    draw.rounded_rectangle(
        (pad - 4, pad - 4, pad + 128 * scale + 3, pad + 64 * scale + 3),
        radius=4,
        fill=(6, 16, 34),
        outline=(56, 76, 104),
        width=2,
    )
    canvas.paste(scaled, (pad, pad))
    return canvas


def capture_real_klipper_lcd(out_path: Path) -> Tuple[int, int, int, int]:
    """Boot real Klipper + UC1701 + prusa_kload_menu.cfg and capture VRAM frames."""
    _ensure_klippy_on_path()
    import klippy as klippy_mod
    import reactor as reactor_mod

    frames: List[Image.Image] = []
    durations: List[int] = []

    with tempfile.TemporaryDirectory() as tmpdir:
        sdcard_dir = os.path.join(tmpdir, "gcodes")
        os.makedirs(sdcard_dir, exist_ok=True)
        with open(os.path.join(sdcard_dir, "petg_bracket.gcode"), "w", encoding="utf-8") as fh:
            fh.write("; filament_type = PETG\nG28\n")

        state_file = os.path.join(tmpdir, "state.json")
        cfg_path = os.path.join(tmpdir, "printer.cfg")
        out_serial = os.path.join(tmpdir, "serial.out")
        sim_dict = _prepare_sim_dict(tmpdir)
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write(
                SIM_PRINTER_CFG.format(
                    sdcard_dir=sdcard_dir,
                    menu_cfg=str(MENU_CFG),
                    state_file=state_file,
                )
            )

        r_fd, w_fd = os.pipe()
        errors: List[BaseException] = []
        try:
            start_args = {
                "config_file": cfg_path,
                "gcode_fd": r_fd,
                "debuginput": "pipe",
                "debugoutput": out_serial,
                "dictionary": sim_dict,
                "start_reason": "startup",
            }
            main_reactor = reactor_mod.Reactor()
            printer = klippy_mod.Printer(main_reactor, None, start_args)

            def on_ready():
                def drive(et):
                    try:
                        disp = printer.lookup_object("display")
                        menu = printer.lookup_object("menu")
                        gcode = printer.lookup_object("gcode")
                        extruder = printer.lookup_object("extruder")
                        heater = extruder.get_heater()
                        bed = printer.lookup_object("heater_bed").heater
                        fs = printer.lookup_object("filament_switch_sensor fsensor")

                        def set_temps(ext_c: float, ext_t: float, bed_c: float = 24.0, bed_t: float = 0.0):
                            heater.smoothed_temp = ext_c
                            heater.target_temp = ext_t
                            bed.smoothed_temp = bed_c
                            bed.target_temp = bed_t

                        def snap(ms: int):
                            now = main_reactor.monotonic()
                            disp.screen_update_event(now)
                            frames.append(_vram_to_image(disp.lcd_chip.vram, scale=4))
                            durations.append(ms)

                        def click_menu():
                            now = main_reactor.monotonic()
                            menu.key_event("click", now)
                            main_reactor.pause(now + 0.08)

                        def select_in_current_list(predicate):
                            now = main_reactor.monotonic()
                            menu.update_context(now)
                            container = menu.stack_peek()
                            container.init()
                            items = list(container)
                            target_idx = None
                            for idx, it in enumerate(items):
                                if predicate(it):
                                    target_idx = idx
                                    break
                            assert target_idx is not None, f"Menu item not found in {[i.get_ns() for i in items]}"
                            container.select_at(target_idx)

                        def enter_prusa_kload_menu():
                            now = main_reactor.monotonic()
                            menu.begin(now)
                            select_in_current_list(lambda it: it.get_ns() == "__main __prusa_kload")
                            click_menu()

                        def select_menu_item(target_suffix: str):
                            select_in_current_list(lambda it: it.get_ns().endswith(target_suffix))

                        # 1. Idle status screen
                        set_temps(25.0, 0.0)
                        gcode.run_script("M117 Ready")
                        snap(1000)

                        # 2. Filament inserted -> Load: Pick Mat
                        fs.runout_helper.note_filament_present(main_reactor.monotonic(), True)
                        main_reactor.pause(main_reactor.monotonic() + 0.12)
                        snap(1000)

                        # 3. Loading PLA -> Heating & Loading status
                        set_temps(215.0, 215.0)
                        gcode.run_script("M117 Heat PLA 215C")
                        snap(850)
                        gcode.run_script("PRUSA_KLOAD_LOAD MATERIAL=PLA")
                        set_temps(215.0, 215.0)
                        snap(1100)  # M117: Color clear?

                        # 4. Open real Klipper menu -> PrusaKLoad -> Color Clear? -> Yes (Color Clear)
                        enter_prusa_kload_menu()
                        snap(900)  # Loaded: None / Color Clear?
                        select_menu_item("__confirm_purge")
                        snap(950)
                        click_menu()
                        snap(750)  # Inside Color Clear? (.. selected)
                        select_menu_item("__yes")
                        snap(1150)  # Yes (Color Clear) selected
                        click_menu()
                        menu.exit()
                        set_temps(210.0, 0.0)
                        snap(1200)  # Status screen: Loaded PLA

                        # 5. Start PETG print -> Material Mismatch!
                        set_temps(28.0, 0.0)
                        gcode.run_script("SDCARD_PRINT_FILE FILENAME=petg_bracket.gcode")
                        snap(1300)  # M117: Mat: PLA!=PETG

                        # 6. Open PrusaKLoad menu -> Loaded: PLA + Material Mismatch! -> Swap -> PETG
                        enter_prusa_kload_menu()
                        snap(900)  # Loaded: PLA
                        select_menu_item("__mismatch")
                        snap(1050)  # Material Mismatch! selected
                        click_menu()
                        select_menu_item("__swap")
                        snap(1250)  # Swap -> PETG selected
                        click_menu()
                        menu.exit()

                        # 7. Swap sequence status lines on real LCD
                        set_temps(215.0, 215.0)
                        snap(900)  # Pull filament out
                        fs.runout_helper.note_filament_present(main_reactor.monotonic(), False)
                        main_reactor.pause(main_reactor.monotonic() + 0.12)
                        snap(950)  # Insert PETG
                        fs.runout_helper.note_filament_present(main_reactor.monotonic(), True)
                        main_reactor.pause(main_reactor.monotonic() + 0.12)
                        set_temps(230.0, 230.0)
                        snap(950)  # Color clear?
                        gcode.run_script("PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES")
                        set_temps(230.0, 230.0, 78.0, 80.0)
                        snap(1800)  # Starting print

                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        printer.request_exit("exit")

                main_reactor.register_callback(drive)

            printer.register_event_handler("klippy:ready", on_ready)
            printer.run()
            if errors:
                raise errors[0]
        finally:
            os.close(r_fd)
            os.close(w_fd)

    return save_optimized_gif(out_path, frames, durations, colors=32)


# ---------------------------------------------------------------------------
# PART 2: Real KlipperScreen (GTK3) + Local Moonraker Bridge + Real Klipper
# ---------------------------------------------------------------------------

WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


class MiniMoonrakerBridge:
    """Minimal Moonraker JSON-RPC HTTP + WebSocket server backed by real Klipper."""

    def __init__(
        self,
        sdcard_dir: str,
        bind_host: str = "127.0.0.1",
        bind_port: int = 0,
        static_dir: Optional[str] = None,
    ):
        self.sdcard_dir = sdcard_dir
        self.static_dir = Path(static_dir).resolve() if static_dir else None
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind((bind_host, bind_port))
        self.port = self.sock.getsockname()[1]
        self.sock.listen(16)

        self._client_conns: List[socket.socket] = []
        self._send_lock = threading.Lock()
        self._stop = threading.Event()
        self._connected_ev = threading.Event()
        self._gcode_handler = None
        self._status_provider = None
        self._objects_provider = None
        self._config_provider = None
        self._sim_control_handler = None
        self._gcode_store: List[Dict[str, Any]] = []
        self._db: Dict[str, Dict[str, Any]] = {
            "mainsail": {
                "general": {"printername": "PrusaKLoad"},
                "uiSettings": {"mode": "dark"},
            }
        }
        self._thread = threading.Thread(target=self._serve_loop, daemon=True)

    def bind_klipper(
        self,
        gcode_handler,
        status_provider,
        objects_provider,
        config_provider,
        sim_control_handler=None,
    ):
        self._gcode_handler = gcode_handler
        self._status_provider = status_provider
        self._objects_provider = objects_provider
        self._config_provider = config_provider
        self._sim_control_handler = sim_control_handler

    def start(self):
        self._thread.start()

    def stop(self):
        self._stop.set()
        try:
            self.sock.close()
        except OSError:
            pass
        with self._send_lock:
            for conn in list(self._client_conns):
                try:
                    conn.close()
                except OSError:
                    pass
            self._client_conns.clear()

    def _ws_send_json(self, payload: Dict[str, Any], target_conn: Optional[socket.socket] = None) -> None:
        data = json.dumps(payload).encode("utf-8")
        length = len(data)
        if length < 126:
            header = struct.pack("!BB", 0x81, length)
        elif length < 65536:
            header = struct.pack("!BBH", 0x81, 126, length)
        else:
            header = struct.pack("!BBQ", 0x81, 127, length)
        frame = header + data
        with self._send_lock:
            targets = [target_conn] if target_conn is not None else list(self._client_conns)
            dead = []
            for conn in targets:
                if conn is None:
                    continue
                try:
                    conn.sendall(frame)
                except OSError:
                    dead.append(conn)
            for d in dead:
                if d in self._client_conns:
                    self._client_conns.remove(d)

    def notify(self, method: str, params: Any) -> None:
        if method == "notify_gcode_response" and params:
            self._gcode_store.append(
                {"message": str(params[0]), "time": time.time(), "type": "response"}
            )
            self._gcode_store = self._gcode_store[-200:]
        self._ws_send_json({"jsonrpc": "2.0", "method": method, "params": params})

    def _recv_exact(self, conn: socket.socket, n: int) -> Optional[bytes]:
        buf = bytearray()
        while len(buf) < n:
            chunk = conn.recv(n - len(buf))
            if not chunk:
                return None
            buf.extend(chunk)
        return bytes(buf)

    def _read_ws_frame(self, conn: socket.socket) -> Optional[Tuple[int, bytes]]:
        hdr = self._recv_exact(conn, 2)
        if not hdr:
            return None
        b1, b2 = hdr[0], hdr[1]
        opcode = b1 & 0x0F
        masked = bool(b2 & 0x80)
        length = b2 & 0x7F
        if length == 126:
            ext = self._recv_exact(conn, 2)
            if not ext:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self._recv_exact(conn, 8)
            if not ext:
                return None
            length = struct.unpack("!Q", ext)[0]
        mask = self._recv_exact(conn, 4) if masked else b""
        payload = self._recv_exact(conn, length) if length else b""
        if payload is None:
            return None
        if masked and mask:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload

    def _serve_loop(self):
        self.sock.settimeout(0.5)
        while not self._stop.is_set():
            try:
                conn, _ = self.sock.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            threading.Thread(target=self._handle_conn, args=(conn,), daemon=True).start()

    def _handle_conn(self, conn: socket.socket):
        try:
            raw = b""
            while b"\r\n\r\n" not in raw:
                chunk = conn.recv(4096)
                if not chunk:
                    conn.close()
                    return
                raw += chunk
            headers_text = raw.split(b"\r\n\r\n", 1)[0].decode("latin1")
            lines = headers_text.split("\r\n")
            req_line = lines[0]
            headers = {}
            for line in lines[1:]:
                if ":" in line:
                    k, v = line.split(":", 1)
                    headers[k.strip().lower()] = v.strip()

            if "upgrade" in headers and headers["upgrade"].lower() == "websocket":
                key = headers["sec-websocket-key"]
                accept = base64.b64encode(
                    hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
                ).decode("ascii")
                resp = (
                    "HTTP/1.1 101 Switching Protocols\r\n"
                    "Upgrade: websocket\r\n"
                    "Connection: Upgrade\r\n"
                    f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
                )
                conn.sendall(resp.encode("ascii"))
                with self._send_lock:
                    self._client_conns.append(conn)
                self._connected_ev.set()
                try:
                    self._ws_loop(conn)
                finally:
                    with self._send_lock:
                        if conn in self._client_conns:
                            self._client_conns.remove(conn)
                    try:
                        conn.close()
                    except OSError:
                        pass
            else:
                self._handle_http(conn, req_line, headers, raw.split(b"\r\n\r\n", 1)[1])
        except Exception:
            try:
                conn.close()
            except OSError:
                pass

    def _send_http(
        self,
        conn: socket.socket,
        status: str,
        body: bytes,
        content_type: str = "application/json",
    ) -> None:
        resp = (
            f"HTTP/1.1 {status}\r\n"
            f"Content-Type: {content_type}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Access-Control-Allow-Origin: *\r\n"
            "Connection: close\r\n\r\n"
        ).encode("ascii") + body
        conn.sendall(resp)
        conn.close()

    def _handle_http(
        self,
        conn: socket.socket,
        req_line: str,
        headers: Dict[str, str],
        initial_body: bytes,
    ) -> None:
        parts = req_line.split(" ")
        method = parts[0] if parts else "GET"
        raw_path = parts[1] if len(parts) > 1 else "/"
        url_path = urlparse(raw_path).path

        content_len = int(headers.get("content-length", "0") or "0")
        body_bytes = bytearray(initial_body)
        while len(body_bytes) < content_len:
            chunk = conn.recv(content_len - len(body_bytes))
            if not chunk:
                break
            body_bytes.extend(chunk)

        if url_path == "/__sim" and method == "POST":
            payload = json.loads(bytes(body_bytes).decode("utf-8") or "{}")
            res = self._sim_control_handler(payload) if self._sim_control_handler else {}
            self._send_http(conn, "200 OK", json.dumps({"ok": True, "result": res}).encode("utf-8"))
            return

        if url_path == "/config.json":
            cfg = {
                "defaultLocale": "en",
                "defaultMode": "dark",
                "defaultTheme": "mainsail",
                "hostname": "127.0.0.1",
                "port": self.port,
                "path": None,
                "instancesDB": "moonraker",
                "instances": [],
            }
            self._send_http(conn, "200 OK", json.dumps(cfg).encode("utf-8"))
            return

        if url_path == "/access/oneshot_token":
            self._send_http(conn, "200 OK", json.dumps({"result": ""}).encode("utf-8"))
            return

        if url_path == "/server/info":
            self._send_http(
                conn,
                "200 OK",
                json.dumps(
                    {
                        "result": {
                            "klippy_connected": True,
                            "klippy_state": "ready",
                            "components": ["database", "history", "announcements", "webcams"],
                            "failed_components": [],
                            "registered_directories": ["gcodes", "config"],
                            "warnings": [],
                            "missing_klippy_requirements": [],
                            "api_version": [1, 4, 0],
                            "api_version_string": "1.4.0",
                        }
                    }
                ).encode("utf-8"),
            )
            return

        if self.static_dir is not None:
            rel = url_path.lstrip("/") or "index.html"
            candidate = (self.static_dir / rel).resolve()
            if not str(candidate).startswith(str(self.static_dir)) or not candidate.is_file():
                candidate = self.static_dir / "index.html"
            if candidate.is_file():
                ctype, _ = mimetypes.guess_type(str(candidate))
                if candidate.suffix == ".js":
                    ctype = "application/javascript"
                elif candidate.suffix == ".css":
                    ctype = "text/css"
                elif candidate.suffix == ".svg":
                    ctype = "image/svg+xml"
                elif candidate.suffix == ".woff2":
                    ctype = "font/woff2"
                self._send_http(conn, "200 OK", candidate.read_bytes(), ctype or "application/octet-stream")
                return

        self._send_http(conn, "200 OK", json.dumps({"result": {}}).encode("utf-8"))

    def _ws_loop(self, conn: socket.socket):
        while not self._stop.is_set():
            frame = self._read_ws_frame(conn)
            if frame is None:
                break
            opcode, payload = frame
            if opcode == 8:  # close
                break
            if opcode == 9:  # ping -> pong
                with self._send_lock:
                    conn.sendall(struct.pack("!BB", 0x8A, len(payload)) + payload)
                continue
            if opcode != 1:
                continue
            try:
                msg = json.loads(payload.decode("utf-8"))
            except Exception:
                continue
            self._handle_rpc(msg, conn)

    def _handle_rpc(self, msg: Dict[str, Any], conn: socket.socket):
        req_id = msg.get("id")
        method = msg.get("method", "")
        params = msg.get("params") or {}
        result: Any = {}

        if method == "server.connection.identify":
            result = {"connection_id": 1}
        elif method == "server.info":
            result = {
                "klippy_connected": True,
                "klippy_state": "ready",
                "components": ["database", "history", "announcements", "webcams"],
                "failed_components": [],
                "registered_directories": ["gcodes", "config"],
                "warnings": [],
                "missing_klippy_requirements": [],
                "api_version": [1, 4, 0],
                "api_version_string": "1.4.0",
            }
        elif method == "printer.info":
            result = {
                "state": "ready",
                "state_message": "Printer is ready",
                "hostname": "prusakload-sim",
                "software_version": "v0.14.0",
            }
        elif method == "printer.objects.list":
            result = {"objects": self._objects_provider() if self._objects_provider else []}
        elif method in ("printer.objects.query", "printer.objects.subscribe"):
            objs = params.get("objects") or {}
            status = self._status_provider(list(objs.keys())) if self._status_provider else {}
            if "configfile" in objs and self._config_provider:
                status["configfile"] = self._config_provider()
            result = {"status": status, "eventtime": time.monotonic()}
        elif method == "printer.gcode.help":
            result = {
                "PRUSA_KLOAD_LOAD": "Load filament",
                "PRUSA_KLOAD_UNLOAD": "Unload filament",
                "PRUSA_KLOAD_SWAP": "Swap filament",
                "PRUSA_KLOAD_STATUS": "Report status",
            }
        elif method == "machine.system_info":
            result = {"system_info": {"cpu_info": {"processor": "x86_64"}, "sd_info": {}, "distribution": {"name": "Linux"}}}
        elif method == "machine.proc_stats":
            result = {
                "moonraker_stats": [],
                "throttled_state": {"bits": 0, "flags": []},
                "cpu_temp": 42.0,
                "network": {},
                "system_cpu_usage": {"cpu": 4.0},
                "system_memory": {"total": 4000000, "available": 3200000, "used": 800000},
                "websocket_connections": len(self._client_conns),
            }
        elif method == "server.config":
            result = {"config": {"data_store": {"temperature_store_size": 1200}}}
        elif method == "server.temperature_store":
            result = {
                "extruder": {"temperatures": [25.0] * 10, "targets": [0.0] * 10, "powers": [0.0] * 10},
                "heater_bed": {"temperatures": [24.0] * 10, "targets": [0.0] * 10, "powers": [0.0] * 10},
            }
        elif method == "server.gcode_store":
            result = {"gcode_store": list(self._gcode_store)}
        elif method == "server.database.list":
            result = {"namespaces": list(self._db.keys())}
        elif method == "server.database.get_item":
            ns = params.get("namespace", "mainsail")
            key = params.get("key")
            ns_data = self._db.get(ns, {})
            if key is None:
                result = {"namespace": ns, "key": None, "value": ns_data}
            else:
                val = ns_data.get(key)
                if val is None:
                    if req_id is not None:
                        self._ws_send_json(
                            {"jsonrpc": "2.0", "id": req_id, "error": {"code": 404, "message": "Key not found"}},
                            target_conn=conn,
                        )
                    return
                result = {"namespace": ns, "key": key, "value": val}
        elif method == "server.database.post_item":
            ns = params.get("namespace", "mainsail")
            key = params.get("key")
            val = params.get("value")
            self._db.setdefault(ns, {})[key] = val
            result = {"namespace": ns, "key": key, "value": val}
        elif method == "server.history.totals":
            result = {
                "job_totals": {
                    "total_jobs": 12,
                    "total_time": 18400,
                    "total_print_time": 17200,
                    "total_filament_used": 42500,
                    "longest_job": 3600,
                    "longest_print": 3400,
                }
            }
        elif method == "server.history.list":
            result = {"count": 0, "jobs": []}
        elif method == "server.announcements.list":
            result = {"entries": [], "feeds": []}
        elif method == "server.webcams.list":
            result = {"webcams": []}
        elif method == "access.oneshot_token":
            result = ""
        elif method == "server.files.roots":
            result = [{"name": "gcodes", "path": self.sdcard_dir, "permissions": "rw"}]
        elif method == "server.files.list":
            result = [
                {
                    "path": "petg_bracket.gcode",
                    "modified": time.time(),
                    "size": 124800,
                    "permissions": "rw",
                }
            ]
        elif method == "server.files.get_directory":
            result = {
                "dirs": [],
                "files": [
                    {
                        "filename": "petg_bracket.gcode",
                        "modified": time.time(),
                        "size": 124800,
                        "permissions": "rw",
                    }
                ],
                "disk_usage": {"total": 10**9, "used": 10**8, "free": 9 * 10**8},
                "root_info": {"name": "gcodes", "permissions": "rw"},
            }
        elif method == "printer.gcode.script":
            script = params.get("script", "")
            if self._gcode_handler:
                self._gcode_handler(script)
            result = "ok"

        if req_id is not None:
            self._ws_send_json({"jsonrpc": "2.0", "id": req_id, "result": result}, target_conn=conn)


class LiveKlipperBackend:
    """Runs real in-process Klipper Printer on its own thread with a command queue."""

    def __init__(self, bridge: MiniMoonrakerBridge, sdcard_dir: str, tmpdir: str):
        self.bridge = bridge
        self.sdcard_dir = sdcard_dir
        self.tmpdir = tmpdir
        self.ready_ev = threading.Event()
        self._cmd_q: "queue.Queue[Tuple[Any, threading.Event, List[Any]]]" = queue.Queue()
        self._stop = False
        self.printer = None
        self.reactor = None
        self._thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        # Import klippy on the main thread (util.py registers SIGINT at import time)
        _ensure_klippy_on_path()
        import klippy as _klippy_mod  # noqa: F401
        import reactor as _reactor_mod  # noqa: F401
        self._thread.start()
        assert self.ready_ev.wait(timeout=15), "Real Klipper failed to reach ready state"

    def call(self, fn):
        ev = threading.Event()
        box: List[Any] = []
        self._cmd_q.put((fn, ev, box))
        ev.wait(timeout=10)
        if box and isinstance(box[0], BaseException):
            raise box[0]
        return box[0] if box else None

    def stop(self):
        self._stop = True
        ev = threading.Event()
        self._cmd_q.put((None, ev, []))
        self._thread.join(timeout=5)

    def _run(self):
        _ensure_klippy_on_path()
        import klippy as klippy_mod
        import reactor as reactor_mod

        state_file = os.path.join(self.tmpdir, "ks_state.json")
        cfg_path = os.path.join(self.tmpdir, "printer.cfg")
        out_serial = os.path.join(self.tmpdir, "serial.out")
        sim_dict = _prepare_sim_dict(self.tmpdir)
        with open(cfg_path, "w", encoding="utf-8") as fh:
            fh.write(
                SIM_PRINTER_CFG.format(
                    sdcard_dir=self.sdcard_dir,
                    menu_cfg=str(MENU_CFG),
                    state_file=state_file,
                )
            )

        r_fd, w_fd = os.pipe()
        try:
            start_args = {
                "config_file": cfg_path,
                "gcode_fd": r_fd,
                "debuginput": "pipe",
                "debugoutput": out_serial,
                "dictionary": sim_dict,
                "start_reason": "startup",
            }
            main_reactor = reactor_mod.Reactor()
            printer = klippy_mod.Printer(main_reactor, None, start_args)
            self.printer = printer
            self.reactor = main_reactor

            def on_ready():
                gcode = printer.lookup_object("gcode")
                orig_raw = gcode.respond_raw
                orig_info = gcode.respond_info

                def hooked_raw(msg):
                    orig_raw(msg)
                    self.bridge.notify("notify_gcode_response", [msg])

                def hooked_info(msg, log=True):
                    orig_info(msg, log=log)
                    self.bridge.notify("notify_gcode_response", [f"// {msg}"])

                gcode.respond_raw = hooked_raw
                gcode.respond_info = hooked_info

                def poll_commands(et):
                    while True:
                        try:
                            fn, ev, box = self._cmd_q.get_nowait()
                        except queue.Empty:
                            break
                        if fn is None:
                            ev.set()
                            printer.request_exit("exit")
                            return main_reactor.NEVER
                        try:
                            box.append(fn(printer, main_reactor))
                        except BaseException as exc:
                            box.append(exc)
                        finally:
                            ev.set()
                    return et + 0.02

                main_reactor.register_timer(poll_commands, main_reactor.NOW)
                self.ready_ev.set()

            printer.register_event_handler("klippy:ready", on_ready)
            printer.run()
        finally:
            os.close(r_fd)
            os.close(w_fd)


def _pixbuf_to_pil(pb) -> Image.Image:
    w, h = pb.get_width(), pb.get_height()
    stride = pb.get_rowstride()
    has_alpha = pb.get_has_alpha()
    mode = "RGBA" if has_alpha else "RGB"
    data = pb.get_pixels()
    img = Image.frombytes(mode, (w, h), data, "raw", mode, stride)
    return img.convert("RGB")


def capture_real_klipperscreen(
    out_load: Path, out_mismatch: Path
) -> Tuple[Tuple[int, int, int, int], Tuple[int, int, int, int]]:
    """Run real KlipperScreen GTK3 connected to real Klipper and capture both GIFs."""
    ks_str = str(KS_DIR)
    if ks_str not in sys.path:
        sys.path.insert(0, ks_str)

    # Use X11/XWayland backend so Gdk.pixbuf_get_from_window reads real pixels
    os.environ["GDK_BACKEND"] = "x11"

    import gi

    gi.require_version("Gtk", "3.0")
    gi.require_version("Gdk", "3.0")
    from gi.repository import Gdk, GLib, Gtk
    import screen as ks_screen_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        sdcard_dir = os.path.join(tmpdir, "gcodes")
        os.makedirs(sdcard_dir, exist_ok=True)
        with open(os.path.join(sdcard_dir, "petg_bracket.gcode"), "w", encoding="utf-8") as fh:
            fh.write("; filament_type = PETG\nG28\n")

        bridge = MiniMoonrakerBridge(sdcard_dir)
        backend = LiveKlipperBackend(bridge, sdcard_dir, tmpdir)
        backend.start()

        def query_status(obj_names: Sequence[str]) -> Dict[str, Any]:
            def _do(printer, mr):
                et = mr.monotonic()
                out: Dict[str, Any] = {}
                for name in obj_names:
                    obj = printer.lookup_object(name, None)
                    if obj is not None and hasattr(obj, "get_status"):
                        try:
                            out[name] = obj.get_status(et)
                        except Exception:
                            out[name] = {}
                out["webhooks"] = {"state": "ready", "state_message": "Printer is ready"}
                return out

            return backend.call(_do)

        def list_objects() -> List[str]:
            def _do(printer, _mr):
                return [
                    "webhooks",
                    "extruder",
                    "heater_bed",
                    "fan",
                    "toolhead",
                    "gcode_move",
                    "display_status",
                    "print_stats",
                    "virtual_sdcard",
                    "pause_resume",
                    "idle_timeout",
                    "filament_switch_sensor fsensor",
                    "prusa_kload",
                ]

            return backend.call(_do)

        def get_configfile() -> Dict[str, Any]:
            return {
                "config": {
                    "printer": {"kinematics": "cartesian", "max_velocity": "300", "max_accel": "3000"},
                    "extruder": {
                        "min_temp": "0",
                        "max_temp": "300",
                        "nozzle_diameter": "0.4",
                        "filament_diameter": "1.75",
                    },
                    "heater_bed": {"min_temp": "0", "max_temp": "130"},
                    "fan": {},
                    "virtual_sdcard": {"path": sdcard_dir},
                    "pause_resume": {},
                    "display_status": {},
                    "filament_switch_sensor fsensor": {"switch_pin": "^!30"},
                    "prusa_kload": {"filament_sensor": "filament_switch_sensor fsensor"},
                },
                "warnings": [],
            }

        def run_gcode_from_ks(script: str) -> None:
            def _do(printer, _mr):
                printer.lookup_object("gcode").run_script(script)

            backend.call(_do)
            push_status()

        def push_status() -> None:
            st = query_status(list_objects())
            bridge.notify("notify_status_update", [st, time.monotonic()])

        bridge.bind_klipper(run_gcode_from_ks, query_status, list_objects, get_configfile)
        bridge.start()

        # Write a clean KlipperScreen.conf pointing at our local bridge
        ks_conf = os.path.join(tmpdir, "KlipperScreen.conf")
        with open(ks_conf, "w", encoding="utf-8") as fh:
            fh.write(
                f"""[main]
theme = material-dark
width = 800
height = 480
screen_blanking = off
use_dpms = False
show_cursor = False

[printer PrusaKLoad]
moonraker_host: 127.0.0.1
moonraker_port: {bridge.port}
"""
            )

        args = argparse.Namespace(configfile=ks_conf, logfile=os.path.join(tmpdir, "ks.log"), monitor=0)
        os.environ.pop("XDG_CURRENT_DESKTOP", None)
        ks = ks_screen_mod.KlipperScreen(args)
        # Keep 800x480 touchscreen layout without OS window chrome, and let
        # KlipperScreen render its native touchscreen prompt title bar!
        ks.set_decorated(False)
        ks.windowed = False
        # Prevent fullscreen() from stretching to the host monitor size
        ks.fullscreen = lambda *a, **kw: None
        orig_dialog = ks.gtk.Dialog

        def sized_dialog(title, buttons, content, callback=None, *dargs):
            # Temporarily let Dialog run without calling dialog.fullscreen()
            # so it stays exact 800x480
            ks.windowed = True
            dlg = orig_dialog(title, buttons, content, callback, *dargs)
            ks.windowed = False
            dlg.set_decorated(False)
            dlg.resize(800, 480)
            return dlg

        ks.gtk.Dialog = sized_dialog
        ks.show_all()

        def pump_gtk(seconds: float = 0.25):
            end = time.monotonic() + seconds
            while time.monotonic() < end:
                while Gtk.events_pending():
                    Gtk.main_iteration_do(False)
                time.sleep(0.015)

        def capture_frame(target_size: Tuple[int, int] = (640, 384)) -> Image.Image:
            pump_gtk(0.22)
            # If a real KlipperScreen Prompt dialog is open, capture it; otherwise capture main window
            win = ks
            if ks.prompt is not None and ks.prompt.prompt is not None and ks.prompt.prompt.get_visible():
                win = ks.prompt.prompt
            gdk_win = win.get_window()
            alloc = win.get_allocation()
            pb = Gdk.pixbuf_get_from_window(gdk_win, 0, 0, alloc.width, alloc.height)
            img = _pixbuf_to_pil(pb)
            if img.size != target_size:
                img = img.resize(target_size, Image.Resampling.LANCZOS)
            return img

        def set_sim_state(
            *,
            ext_c: Optional[float] = None,
            ext_t: Optional[float] = None,
            bed_c: Optional[float] = None,
            bed_t: Optional[float] = None,
            print_state: Optional[str] = None,
            filename: Optional[str] = None,
            progress: Optional[float] = None,
            m117: Optional[str] = None,
        ):
            def _do(printer, _mr):
                if ext_c is not None:
                    printer.lookup_object("extruder").get_heater().smoothed_temp = ext_c
                if ext_t is not None:
                    printer.lookup_object("extruder").get_heater().target_temp = ext_t
                if bed_c is not None:
                    printer.lookup_object("heater_bed").heater.smoothed_temp = bed_c
                if bed_t is not None:
                    printer.lookup_object("heater_bed").heater.target_temp = bed_t
                if m117 is not None:
                    printer.lookup_object("gcode").run_script(f"M117 {m117}")
                if print_state is not None:
                    ps = printer.lookup_object("print_stats")
                    if print_state == "printing":
                        ps.set_current_file(filename or "petg_bracket.gcode")
                        ps.note_start()
                    elif print_state == "standby":
                        ps.reset()
                if progress is not None:
                    vsd = printer.lookup_object("virtual_sdcard")
                    vsd.file_size = 1000
                    vsd.file_position = int(1000 * progress)

            backend.call(_do)
            push_status()

        def trigger_sensor(present: bool):
            def _do(printer, mr):
                fs = printer.lookup_object("filament_switch_sensor fsensor")
                fs.runout_helper.note_filament_present(mr.monotonic(), present)
                mr.pause(mr.monotonic() + 0.12)

            backend.call(_do)
            push_status()

        def click_prompt_button_highlight(label_text: str) -> Optional[Image.Image]:
            """Find real GTK button in active prompt, highlight it, and capture."""
            if ks.prompt is None or ks.prompt.prompt is None:
                return None

            def walk(widget):
                if isinstance(widget, Gtk.Button):
                    lbl = widget.get_label()
                    if lbl and lbl.strip() == label_text:
                        return widget
                    for ch in widget.get_children():
                        if isinstance(ch, Gtk.Label) and ch.get_text().strip() == label_text:
                            return widget
                if isinstance(widget, Gtk.Container):
                    for ch in widget.get_children():
                        found = walk(ch)
                        if found is not None:
                            return found
                return None

            btn = walk(ks.prompt.prompt)
            if btn is not None:
                btn.set_state_flags(Gtk.StateFlags.ACTIVE | Gtk.StateFlags.PRELIGHT, False)
                btn.queue_draw()
                img = capture_frame()
                btn.unset_state_flags(Gtk.StateFlags.ACTIVE | Gtk.StateFlags.PRELIGHT)
                return img
            return None

        # Wait for KlipperScreen to finish connecting and reach main_menu
        for _ in range(80):
            pump_gtk(0.1)
            if ks.state.initialized and "main_menu" in ks._cur_panels:
                break
        assert ks.state.initialized, "KlipperScreen did not finish initializing"
        pump_gtk(0.4)

        # ===================================================================
        # SCENARIO 1: assets/klipperscreen-load.gif
        # ===================================================================
        load_frames: List[Image.Image] = []
        load_durs: List[int] = []

        def add_load(ms: int):
            load_frames.append(capture_frame())
            load_durs.append(ms)

        # 1. Idle KlipperScreen main menu
        set_sim_state(ext_c=25.0, ext_t=0.0, bed_c=24.0, bed_t=0.0, m117="Ready")
        add_load(1400)

        # 2. Filament inserted -> real PrusaKLoad: Select Filament prompt!
        trigger_sensor(True)
        add_load(1700)

        # 3. Highlight & tap PLA on real prompt
        hi = click_prompt_button_highlight("PLA")
        if hi:
            load_frames.append(hi)
            load_durs.append(450)

        # 4. Heating & loading progress on KlipperScreen
        # Close prompt first, show real KlipperScreen heating/loading temps
        ks.prompt.end() if ks.prompt else None
        for temp in (95.0, 165.0, 215.0):
            set_sim_state(ext_c=temp, ext_t=215.0, m117="Heat PLA 215C" if temp < 215 else "Loading PLA")
            add_load(550)

        # 5. Real PRUSA_KLOAD_LOAD MATERIAL=PLA -> real Purge Check (PLA) prompt!
        run_gcode_from_ks("PRUSA_KLOAD_LOAD MATERIAL=PLA")
        set_sim_state(ext_c=215.0, ext_t=215.0)
        add_load(1900)

        # 6. Highlight & tap Yes (Color Clear)
        hi = click_prompt_button_highlight("Yes (Color Clear)")
        if hi:
            load_frames.append(hi)
            load_durs.append(450)

        # 7. Confirm purge -> back to KlipperScreen main menu, cooling down
        run_gcode_from_ks("PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES")
        set_sim_state(ext_c=212.0, ext_t=0.0)
        add_load(2000)

        # ===================================================================
        # SCENARIO 2: assets/klipperscreen-mismatch.gif
        # ===================================================================
        mis_frames: List[Image.Image] = []
        mis_durs: List[int] = []

        def add_mis(ms: int):
            mis_frames.append(capture_frame())
            mis_durs.append(ms)

        # 1. Idle KlipperScreen with PLA loaded
        set_sim_state(ext_c=26.0, ext_t=0.0, bed_c=24.0, bed_t=0.0, m117="Loaded PLA")
        add_mis(1200)

        # 2. Start PETG print -> real Material Mismatch! prompt!
        run_gcode_from_ks("SDCARD_PRINT_FILE FILENAME=petg_bracket.gcode")
        add_mis(2000)

        # 3. Highlight & tap Swap Filament
        hi = click_prompt_button_highlight("Swap Filament")
        if hi:
            mis_frames.append(hi)
            mis_durs.append(450)

        # 4. Execute real SWAP -> unloads PLA -> Remove Filament prompt!
        run_gcode_from_ks("PRUSA_KLOAD_MISMATCH_ACTION ACTION=SWAP")
        set_sim_state(ext_c=215.0, ext_t=215.0)
        add_mis(1500)

        # 5. Pull filament out -> real Insert PETG prompt!
        trigger_sensor(False)
        add_mis(1500)

        # 6. Insert PETG -> real load PETG (230C) -> real Purge Check (PETG) prompt!
        trigger_sensor(True)
        set_sim_state(ext_c=230.0, ext_t=230.0)
        add_mis(1800)

        # 7. Highlight & tap Yes (Color Clear)
        hi = click_prompt_button_highlight("Yes (Color Clear)")
        if hi:
            mis_frames.append(hi)
            mis_durs.append(450)

        # 8. Confirm purge -> print starts! Show real KlipperScreen job_status panel
        run_gcode_from_ks("PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES")
        set_sim_state(
            ext_c=230.0,
            ext_t=230.0,
            bed_c=78.0,
            bed_t=80.0,
            print_state="printing",
            filename="petg_bracket.gcode",
            progress=0.04,
            m117="Starting print",
        )
        add_mis(2200)

        ks.destroy()
        bridge.stop()
        backend.stop()

    info_load = save_optimized_gif(out_load, load_frames, load_durs, colors=128)
    info_mis = save_optimized_gif(out_mismatch, mis_frames, mis_durs, colors=128)
    return info_load, info_mis


def serve_mainsail_bridge(mainsail_dir: str, port: int = 7125) -> None:
    """Serve real Mainsail static files + Moonraker RPC backed by real Klipper."""
    with tempfile.TemporaryDirectory() as tmpdir:
        sdcard_dir = os.path.join(tmpdir, "gcodes")
        os.makedirs(sdcard_dir, exist_ok=True)
        with open(os.path.join(sdcard_dir, "petg_bracket.gcode"), "w", encoding="utf-8") as fh:
            fh.write("; filament_type = PETG\nG28\n")

        bridge = MiniMoonrakerBridge(
            sdcard_dir,
            bind_host="0.0.0.0",
            bind_port=port,
            static_dir=mainsail_dir,
        )
        backend = LiveKlipperBackend(bridge, sdcard_dir, tmpdir)
        backend.start()

        def query_status(obj_names: Sequence[str]) -> Dict[str, Any]:
            def _do(printer, mr):
                et = mr.monotonic()
                out: Dict[str, Any] = {}
                for name in obj_names:
                    obj = printer.lookup_object(name, None)
                    if obj is not None and hasattr(obj, "get_status"):
                        try:
                            out[name] = obj.get_status(et)
                        except Exception:
                            out[name] = {}
                out["webhooks"] = {"state": "ready", "state_message": "Printer is ready"}
                return out

            return backend.call(_do)

        def list_objects() -> List[str]:
            return [
                "webhooks",
                "extruder",
                "heater_bed",
                "fan",
                "toolhead",
                "gcode_move",
                "display_status",
                "print_stats",
                "virtual_sdcard",
                "pause_resume",
                "idle_timeout",
                "filament_switch_sensor fsensor",
                "prusa_kload",
            ]

        def get_configfile() -> Dict[str, Any]:
            return {
                "config": {
                    "printer": {"kinematics": "cartesian", "max_velocity": "300", "max_accel": "3000"},
                    "extruder": {
                        "min_temp": "0",
                        "max_temp": "300",
                        "nozzle_diameter": "0.4",
                        "filament_diameter": "1.75",
                    },
                    "heater_bed": {"min_temp": "0", "max_temp": "130"},
                    "fan": {},
                    "virtual_sdcard": {"path": sdcard_dir},
                    "pause_resume": {},
                    "display_status": {},
                    "filament_switch_sensor fsensor": {"switch_pin": "^!30"},
                    "prusa_kload": {"filament_sensor": "filament_switch_sensor fsensor"},
                },
                "settings": {
                    "printer": {"kinematics": "cartesian", "max_velocity": 300.0, "max_accel": 3000.0},
                    "extruder": {"min_temp": 0.0, "max_temp": 300.0, "nozzle_diameter": 0.4, "filament_diameter": 1.75},
                    "heater_bed": {"min_temp": 0.0, "max_temp": 130.0},
                },
                "warnings": [],
            }

        def push_status() -> None:
            st = query_status(list_objects())
            bridge.notify("notify_status_update", [st, time.monotonic()])

        def run_gcode(script: str) -> None:
            def _do(printer, _mr):
                printer.lookup_object("gcode").run_script(script)

            backend.call(_do)
            push_status()

        def handle_sim_control(payload: Dict[str, Any]) -> Dict[str, Any]:
            action = payload.get("action")
            if action == "sensor":
                present = bool(payload.get("present"))

                def _do(printer, mr):
                    fs = printer.lookup_object("filament_switch_sensor fsensor")
                    fs.runout_helper.note_filament_present(mr.monotonic(), present)
                    mr.pause(mr.monotonic() + 0.12)

                backend.call(_do)
                push_status()
            elif action == "gcode":
                run_gcode(str(payload.get("script", "")))
            elif action == "set_temps":
                ext_c = payload.get("ext_c")
                ext_t = payload.get("ext_t")
                bed_c = payload.get("bed_c")
                bed_t = payload.get("bed_t")
                m117 = payload.get("m117")

                def _do(printer, _mr):
                    if ext_c is not None:
                        printer.lookup_object("extruder").get_heater().smoothed_temp = float(ext_c)
                    if ext_t is not None:
                        printer.lookup_object("extruder").get_heater().target_temp = float(ext_t)
                    if bed_c is not None:
                        printer.lookup_object("heater_bed").heater.smoothed_temp = float(bed_c)
                    if bed_t is not None:
                        printer.lookup_object("heater_bed").heater.target_temp = float(bed_t)
                    if m117 is not None:
                        printer.lookup_object("gcode").run_script(f"M117 {m117}")

                backend.call(_do)
                push_status()
            elif action == "close_prompt":
                bridge.notify("notify_gcode_response", ["// action:prompt_end"])
            elif action == "shutdown":
                threading.Timer(0.1, bridge.stop).start()
            return query_status(["prusa_kload", "extruder", "display_status"])

        bridge.bind_klipper(run_gcode, query_status, list_objects, get_configfile, handle_sim_control)
        bridge.start()
        print(f"MAINSAIL_BRIDGE_READY port={bridge.port}", flush=True)
        try:
            while not bridge._stop.is_set():
                time.sleep(0.2)
        except KeyboardInterrupt:
            pass
        finally:
            bridge.stop()
            backend.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--lcd-only", action="store_true")
    parser.add_argument("--ks-only", action="store_true")
    parser.add_argument("--serve-mainsail", action="store_true")
    parser.add_argument("--mainsail-dir", default=os.environ.get("MAINSAIL_DIR", "/tmp/mainsail"))
    parser.add_argument("--port", type=int, default=7125)
    args = parser.parse_args()

    if args.serve_mainsail:
        serve_mainsail_bridge(args.mainsail_dir, port=args.port)
        return 0

    if not args.ks_only:
        w, h, n, sz = capture_real_klipper_lcd(ASSETS_DIR / "printer-display-lcd.gif")
        print(f"Captured real Klipper UC1701 LCD -> assets/printer-display-lcd.gif: {w}x{h}, {n} frames, {sz/1024:.1f} KB")

    if not args.lcd_only:
        load_info, mis_info = capture_real_klipperscreen(
            ASSETS_DIR / "klipperscreen-load.gif",
            ASSETS_DIR / "klipperscreen-mismatch.gif",
        )
        print(
            f"Captured real KlipperScreen Load -> assets/klipperscreen-load.gif: "
            f"{load_info[0]}x{load_info[1]}, {load_info[2]} frames, {load_info[3]/1024:.1f} KB"
        )
        print(
            f"Captured real KlipperScreen Mismatch -> assets/klipperscreen-mismatch.gif: "
            f"{mis_info[0]}x{mis_info[1]}, {mis_info[2]} frames, {mis_info[3]/1024:.1f} KB"
        )
    return 0


if __name__ == "__main__":
    sys.exit(main())
