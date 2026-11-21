#!/usr/bin/env python3
"""Real Klipper (klippy.py) batch simulator integration test suite for PrusaKLoad.

Runs upstream klippy/klippy.py with Klipper's official atmega2560 MCU dictionary,
loading prusa_kload.py, prusa_kload_material.py, and config/prusa_kload_menu.cfg
against real Klipper kinematics, extruder, heaters, virtual_sdcard, pause_resume,
display/menu, and filament_switch_sensor modules.
"""

import json
import os
import subprocess
import sys
import tempfile

REPO_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
MENU_CFG = os.path.join(REPO_DIR, "config", "prusa_kload_menu.cfg")
KLIPPER_DIR = os.environ.get("KLIPPER_DIR", "/tmp/klipper-sim")
DICT_PATH = os.environ.get(
    "KLIPPER_DICT", os.path.join(KLIPPER_DIR, "out", "klipper.dict")
)
KLIPPY_PY = os.path.join(KLIPPER_DIR, "klippy", "klippy.py")


BASE_PRINTER_CFG = """
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
lcd_type: hd44780
rs_pin: 20
e_pin: 21
d4_pin: 22
d5_pin: 23
d6_pin: 24
d7_pin: 25
encoder_pins: ^26, ^27
click_pin: ^!28

[include {menu_cfg}]

{sensor_block}

[prusa_kload]
{kload_sensor_line}
state_file: {state_file}
debounce_delay: 0.1
load_distance: 60.0
unload_distance: 80.0
ramming_distance: 10.0
cooling_moves: 1
prompt_timeout: 300.0

[prusa_kload_material PLA]
load_temp: 210
aliases: PLA+, MATTE PLA

[prusa_kload_material CF-PA]
load_temp: 285
unload_temp: 280
purge_distance: 35.0
aliases: PAHT-CF

[gcode_macro PRINT_START]
gcode:
    {{% set MATERIAL = params.MATERIAL|default("")|string %}}
    {{% if MATERIAL %}}
        PRUSA_KLOAD_CHECK MATERIAL={{MATERIAL}}
    {{% endif %}}
"""


def run_klippy_batch(
    tmpdir,
    gcode_lines,
    standalone_pin=False,
    sensor_block_override=None,
    kload_sensor_override=None,
):
    sdcard_dir = os.path.join(tmpdir, "gcodes")
    os.makedirs(sdcard_dir, exist_ok=True)
    state_file = os.path.join(tmpdir, "prusa_kload_state.json")
    cfg_path = os.path.join(tmpdir, "printer.cfg")
    gcode_path = os.path.join(tmpdir, "input.gcode")
    out_path = os.path.join(tmpdir, "serial.out")
    log_path = os.path.join(tmpdir, "klippy.log")

    if sensor_block_override is not None:
        sensor_block = sensor_block_override
    elif standalone_pin:
        sensor_block = ""
    else:
        sensor_block = "[filament_switch_sensor fsensor]\nswitch_pin: ^!30\n"

    if kload_sensor_override is not None:
        kload_sensor_line = kload_sensor_override
    elif standalone_pin:
        kload_sensor_line = "switch_pin: ^!30"
    else:
        kload_sensor_line = "filament_sensor: filament_switch_sensor fsensor"

    cfg_text = BASE_PRINTER_CFG.format(
        sdcard_dir=sdcard_dir,
        menu_cfg=MENU_CFG,
        sensor_block=sensor_block,
        kload_sensor_line=kload_sensor_line,
        state_file=state_file,
    )
    with open(cfg_path, "w", encoding="utf-8") as handle:
        handle.write(cfg_text)
    with open(gcode_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(gcode_lines) + "\n")

    sim_dict_path = os.path.join(tmpdir, "sim.dict")
    with open(DICT_PATH, "r", encoding="utf-8") as handle:
        dict_data = json.load(handle)
    dict_cfg = dict_data.setdefault("config", {})
    dict_cfg.setdefault("ADC_MAX", 1023)
    dict_cfg.setdefault("PWM_MAX", 255)
    with open(sim_dict_path, "w", encoding="utf-8") as handle:
        json.dump(dict_data, handle)

    cmd = [
        sys.executable,
        KLIPPY_PY,
        cfg_path,
        "-d",
        sim_dict_path,
        "-i",
        gcode_path,
        "-o",
        out_path,
        "-l",
        log_path,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    log_text = ""
    if os.path.exists(log_path):
        with open(log_path, "r", encoding="utf-8", errors="ignore") as handle:
            log_text = handle.read()
    state_data = None
    if os.path.exists(state_file):
        with open(state_file, "r", encoding="utf-8") as handle:
            state_data = json.load(handle)
    return proc, log_text, state_data, sdcard_dir


def assert_clean_run(name, proc, log_text):
    if proc.returncode != 0:
        raise AssertionError(
            f"[{name}] klippy exited with {proc.returncode}\n"
            f"STDOUT:\n{proc.stdout}\nSTDERR:\n{proc.stderr}\nLOG:\n{log_text[-3000:]}"
        )
    for bad in ("Traceback (most recent call last):", "Config error", "Internal error"):
        if bad in log_text:
            raise AssertionError(f"[{name}] Found '{bad}' in klippy.log:\n{log_text[-3000:]}")
    if "GCodeError" in log_text or "CommandError" in log_text:
        raise AssertionError(f"[{name}] Found command error in klippy.log:\n{log_text[-3000:]}")


def test_scenario_1_boot_menus_and_commands():
    with tempfile.TemporaryDirectory() as tmpdir:
        proc, log_text, state_data, _ = run_klippy_batch(
            tmpdir,
            [
                "PRUSA_KLOAD_STATUS",
                "PRUSA_KLOAD_LIST_MATERIALS",
                "PRUSA_KLOAD_SET_FILAMENT MATERIAL=matte_pla_nope",
                "PRUSA_KLOAD_SET_FILAMENT MATERIAL=PAHT-CF",
                "PRUSA_KLOAD_GET_FILAMENT",
                "PRUSA_KLOAD_STATUS",
            ],
        )
        assert_clean_run("boot_menus_and_commands", proc, log_text)
        assert state_data is not None and state_data["loaded_filament"] == "CF-PA", state_data
    print("PASS: Scenario 1 — Real Klipper boot, LCD [menu] parse, material overrides & commands")


def test_scenario_2_full_load_purge_and_chunked_unload():
    with tempfile.TemporaryDirectory() as tmpdir:
        # Load 60mm + purge 30mm + purge_more 20mm + unload 80mm on a 50mm max_extrude_only_distance extruder!
        proc, log_text, state_data, _ = run_klippy_batch(
            tmpdir,
            [
                "PRUSA_KLOAD_LOAD MATERIAL=PLA",
                "PRUSA_KLOAD_CONFIRM_PURGE ACTION=PURGE_MORE",
                "PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES",
                "PRUSA_KLOAD_STATUS",
            ],
        )
        assert_clean_run("full_load_purge", proc, log_text)
        assert state_data is not None and state_data["loaded_filament"] == "PLA", state_data

        # Now run unload in the same tmpdir (loads persisted PLA, unloads 80mm in chunks, clears state)
        proc2, log_text2, state_data2, _ = run_klippy_batch(
            tmpdir,
            [
                "PRUSA_KLOAD_UNLOAD",
                "PRUSA_KLOAD_UNLOAD CONFIRM=1",
                "PRUSA_KLOAD_STATUS",
            ],
        )
        assert_clean_run("chunked_unload", proc2, log_text2)
        assert state_data2 is not None and state_data2["loaded_filament"] is None, state_data2
    print("PASS: Scenario 2 — Real Klipper MK4S load, purge_more, confirm, and 80mm chunked unload")


def test_scenario_3_sdcard_intercept_force_cancel_and_swap():
    with tempfile.TemporaryDirectory() as tmpdir:
        sdcard_dir = os.path.join(tmpdir, "gcodes")
        os.makedirs(sdcard_dir, exist_ok=True)
        with open(os.path.join(sdcard_dir, "pla_part.gcode"), "w", encoding="utf-8") as f:
            f.write("; filament_type = PLA+\nG28\nG1 X10 Y10 F1200\n")
        with open(os.path.join(sdcard_dir, "petg_part.gcode"), "w", encoding="utf-8") as f:
            f.write("; filament_type = PETG\nG28\nG1 X20 Y20 F1200\n")

        # 3a: Matching SD file runs cleanly
        proc, log_text, state_data, _ = run_klippy_batch(
            tmpdir,
            [
                "PRUSA_KLOAD_SET_FILAMENT MATERIAL=PLA",
                "SDCARD_PRINT_FILE FILENAME=pla_part.gcode",
            ],
        )
        assert_clean_run("sdcard_match", proc, log_text)
        assert state_data["loaded_filament"] == "PLA"

        # 3b: Mismatch -> Cancel, then Mismatch -> Force Continue
        proc2, log_text2, _, _ = run_klippy_batch(
            tmpdir,
            [
                "SDCARD_PRINT_FILE FILENAME=petg_part.gcode",
                "PRUSA_KLOAD_MISMATCH_ACTION ACTION=CANCEL",
                "SDCARD_PRINT_FILE FILENAME=petg_part.gcode",
                "PRUSA_KLOAD_MISMATCH_ACTION ACTION=FORCE",
            ],
        )
        assert_clean_run("sdcard_mismatch_cancel_force", proc2, log_text2)

        # 3c: Mismatch -> Swap -> (auto-unloads PLA since sensor=False -> SWAP_WAIT_INSERT)
        #     -> Load PETG -> Confirm Purge YES -> Auto-starts print!
        proc3, log_text3, state_data3, _ = run_klippy_batch(
            tmpdir,
            [
                "SDCARD_PRINT_FILE FILENAME=petg_part.gcode",
                "PRUSA_KLOAD_MISMATCH_ACTION ACTION=SWAP",
                "PRUSA_KLOAD_LOAD MATERIAL=PETG",
                "PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES",
            ],
        )
        assert_clean_run("sdcard_mismatch_swap", proc3, log_text3)
        assert state_data3["loaded_filament"] == "PETG", state_data3
    print("PASS: Scenario 3 — Real Klipper SDCARD_PRINT_FILE match, cancel, force, and full swap")


def test_scenario_4_print_start_check_and_standalone_pin():
    with tempfile.TemporaryDirectory() as tmpdir:
        proc, log_text, state_data, _ = run_klippy_batch(
            tmpdir,
            [
                "PRUSA_KLOAD_SET_FILAMENT MATERIAL=PLA",
                "PRINT_START MATERIAL=PLA",
                "PRINT_START MATERIAL=ASA",
                "PRUSA_KLOAD_MISMATCH_ACTION ACTION=CANCEL",
                "PRUSA_KLOAD_LOAD MATERIAL=CF-PA",
                "PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES",
            ],
            standalone_pin=True,
        )
        assert_clean_run("print_start_and_standalone_pin", proc, log_text)
        assert state_data["loaded_filament"] == "CF-PA", state_data
    print("PASS: Scenario 4 — Real Klipper PRINT_START check & standalone switch_pin mode")


def test_scenario_5_retry_swap_cancel_and_unknown_unload():
    with tempfile.TemporaryDirectory() as tmpdir:
        # 5a: Load PLA -> RETRY -> YES, then PRUSA_KLOAD_SWAP MATERIAL=PETG -> Load PETG -> YES
        proc, log_text, state_data, _ = run_klippy_batch(
            tmpdir,
            [
                "PRUSA_KLOAD_LOAD MATERIAL=PLA",
                "PRUSA_KLOAD_CONFIRM_PURGE ACTION=RETRY",
                "PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES",
                "PRUSA_KLOAD_SWAP MATERIAL=PETG",
                "PRUSA_KLOAD_LOAD MATERIAL=PETG",
                "PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES",
            ],
        )
        assert_clean_run("retry_and_swap", proc, log_text)
        assert state_data is not None and state_data["loaded_filament"] == "PETG", state_data

        # 5b: Cancel while in CONFIRM_PURGE leaves loaded_filament unchanged (PETG),
        #     then clear filament and unload when loaded_filament is unknown -> prompts -> pick material -> unloads
        proc2, log_text2, state_data2, _ = run_klippy_batch(
            tmpdir,
            [
                "PRUSA_KLOAD_LOAD MATERIAL=ABS",
                "PRUSA_KLOAD_CANCEL",
                "PRUSA_KLOAD_CLEAR_FILAMENT",
                "PRUSA_KLOAD_UNLOAD",
                "PRUSA_KLOAD_UNLOAD MATERIAL=PETG",
                "PRUSA_KLOAD_STATUS",
            ],
        )
        assert_clean_run("cancel_and_unknown_unload", proc2, log_text2)
        assert state_data2 is not None and state_data2["loaded_filament"] is None, state_data2
    print("PASS: Scenario 5 — Real Klipper RETRY, PRUSA_KLOAD_SWAP, CANCEL, and unknown-material unload")


def test_scenario_6_invalid_config_rejection():
    # 6a: Both filament_sensor and switch_pin specified
    with tempfile.TemporaryDirectory() as tmpdir:
        proc, log_text, _, _ = run_klippy_batch(
            tmpdir,
            ["PRUSA_KLOAD_STATUS"],
            sensor_block_override="[filament_switch_sensor fsensor]\nswitch_pin: ^!30\n",
            kload_sensor_override=(
                "filament_sensor: filament_switch_sensor fsensor\nswitch_pin: ^!31"
            ),
        )
        assert proc.returncode != 0, "Expected Klipper config error when both sensor options set"
        assert "cannot specify both 'filament_sensor' and 'switch_pin'" in log_text, log_text[-2000:]

    # 6b: Neither filament_sensor nor switch_pin specified
    with tempfile.TemporaryDirectory() as tmpdir:
        proc, log_text, _, _ = run_klippy_batch(
            tmpdir,
            ["PRUSA_KLOAD_STATUS"],
            sensor_block_override="",
            kload_sensor_override="",
        )
        assert proc.returncode != 0, "Expected Klipper config error when neither sensor option set"
        assert "must specify either 'filament_sensor' or 'switch_pin'" in log_text, log_text[-2000:]
    print("PASS: Scenario 6 — Real Klipper rejects invalid configs (both or neither sensor option)")


def run_klippy_inprocess(driver_fn, standalone_pin=False):
    klippy_dir = os.path.join(KLIPPER_DIR, "klippy")
    if klippy_dir not in sys.path:
        sys.path.insert(0, klippy_dir)
    import klippy as klippy_mod
    import reactor as reactor_mod

    with tempfile.TemporaryDirectory() as tmpdir:
        sdcard_dir = os.path.join(tmpdir, "gcodes")
        os.makedirs(sdcard_dir, exist_ok=True)
        state_file = os.path.join(tmpdir, "prusa_kload_state.json")
        cfg_path = os.path.join(tmpdir, "printer.cfg")
        out_path = os.path.join(tmpdir, "serial.out")
        sim_dict_path = os.path.join(tmpdir, "sim.dict")

        with open(DICT_PATH, "r", encoding="utf-8") as handle:
            dict_data = json.load(handle)
        dict_cfg = dict_data.setdefault("config", {})
        dict_cfg.setdefault("ADC_MAX", 1023)
        dict_cfg.setdefault("PWM_MAX", 255)
        with open(sim_dict_path, "w", encoding="utf-8") as handle:
            json.dump(dict_data, handle)

        if standalone_pin:
            sensor_block = ""
            kload_sensor_line = "switch_pin: ^!30"
        else:
            sensor_block = "[filament_switch_sensor fsensor]\nswitch_pin: ^!30\n"
            kload_sensor_line = "filament_sensor: filament_switch_sensor fsensor"

        cfg_text = BASE_PRINTER_CFG.format(
            sdcard_dir=sdcard_dir,
            menu_cfg=MENU_CFG,
            sensor_block=sensor_block,
            kload_sensor_line=kload_sensor_line,
            state_file=state_file,
        )
        with open(cfg_path, "w", encoding="utf-8") as handle:
            handle.write(cfg_text)

        r_fd, w_fd = os.pipe()
        errors = []
        try:
            start_args = {
                "config_file": cfg_path,
                "gcode_fd": r_fd,
                "debuginput": "pipe",
                "debugoutput": out_path,
                "dictionary": sim_dict_path,
                "start_reason": "startup",
            }
            main_reactor = reactor_mod.Reactor()
            printer = klippy_mod.Printer(main_reactor, None, start_args)

            def on_ready():
                def drive(_eventtime):
                    try:
                        driver_fn(printer, main_reactor, sdcard_dir, state_file)
                    except BaseException as exc:
                        errors.append(exc)
                    finally:
                        printer.request_exit("exit")

                main_reactor.register_callback(drive)

            printer.register_event_handler("klippy:ready", on_ready)
            res = printer.run()
            if errors:
                raise errors[0]
            assert res == "exit", f"Unexpected klippy exit result: {res}"
        finally:
            os.close(r_fd)
            os.close(w_fd)


def test_scenario_7_live_reactor_sensor_debounce_isolation_and_cancel():
    def driver(printer, main_reactor, _sdcard_dir, state_file):
        kload = printer.lookup_object("prusa_kload")
        fs = printer.lookup_object("filament_switch_sensor fsensor")
        gcode = printer.lookup_object("gcode")
        print_stats = printer.lookup_object("print_stats")
        extruder = printer.lookup_object("extruder")
        heater = extruder.get_heater()

        # 7a: Chatter shorter than debounce_delay (0.1s) must NOT trigger SELECT_MATERIAL
        fs.runout_helper.note_filament_present(main_reactor.monotonic(), True)
        main_reactor.pause(main_reactor.monotonic() + 0.03)
        fs.runout_helper.note_filament_present(main_reactor.monotonic(), False)
        main_reactor.pause(main_reactor.monotonic() + 0.2)
        assert kload.state == "IDLE", f"Chatter unexpectedly changed state: {kload.state}"

        # 7b: Real debounced insert -> SELECT_MATERIAL -> load -> CONFIRM_PURGE -> YES -> remove -> auto-clear
        fs.runout_helper.note_filament_present(main_reactor.monotonic(), True)
        main_reactor.pause(main_reactor.monotonic() + 0.25)
        assert kload.state == "SELECT_MATERIAL", f"Expected SELECT_MATERIAL, got {kload.state}"

        gcode.run_script("PRUSA_KLOAD_LOAD MATERIAL=PLA")
        assert kload.state == "CONFIRM_PURGE", f"Expected CONFIRM_PURGE, got {kload.state}"
        assert heater.get_status(main_reactor.monotonic())["target"] == 210.0

        gcode.run_script("PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES")
        assert kload.state == "IDLE", f"Expected IDLE, got {kload.state}"
        assert kload.loaded_filament == "PLA"
        assert heater.get_status(main_reactor.monotonic())["target"] == 0.0

        fs.runout_helper.note_filament_present(main_reactor.monotonic(), False)
        main_reactor.pause(main_reactor.monotonic() + 0.25)
        assert kload.loaded_filament is None, f"Expected auto-clear, got {kload.loaded_filament}"
        with open(state_file, "r", encoding="utf-8") as handle:
            assert json.load(handle)["loaded_filament"] is None

        # 7c: Mid-print runout/insert isolation through real print_stats + RunoutHelper
        gcode.run_script("PRUSA_KLOAD_SET_FILAMENT MATERIAL=PETG")
        fs.runout_helper.note_filament_present(main_reactor.monotonic(), True)
        main_reactor.pause(main_reactor.monotonic() + 0.25)
        assert kload.state == "IDLE"

        print_stats.note_start()
        assert print_stats.get_status(main_reactor.monotonic())["state"] == "printing"

        # Mid-print runout edge must be ignored by PrusaKLoad
        fs.runout_helper.note_filament_present(main_reactor.monotonic(), False)
        main_reactor.pause(main_reactor.monotonic() + 0.25)
        assert kload.loaded_filament == "PETG", "Mid-print runout cleared loaded_filament!"
        assert kload.state == "IDLE"

        # Mid-print insert edge must also be ignored
        fs.runout_helper.note_filament_present(main_reactor.monotonic(), True)
        main_reactor.pause(main_reactor.monotonic() + 0.25)
        assert kload.state == "IDLE", f"Mid-print insert triggered state {kload.state}"

        print_stats.note_complete()

        # 7d: Unload when loaded_filament is unknown prompts for unload material
        gcode.run_script("PRUSA_KLOAD_CLEAR_FILAMENT")
        gcode.run_script("PRUSA_KLOAD_UNLOAD")
        assert kload.state == "SELECT_MATERIAL", f"Expected SELECT_MATERIAL, got {kload.state}"
        assert kload._select_mode == "unload"
        gcode.run_script("PRUSA_KLOAD_UNLOAD MATERIAL=PLA")
        assert kload.state == "WAIT_REMOVE", f"Expected WAIT_REMOVE, got {kload.state}"
        fs.runout_helper.note_filament_present(main_reactor.monotonic(), False)
        main_reactor.pause(main_reactor.monotonic() + 0.25)
        assert kload.state == "IDLE", f"Expected IDLE after sensor release, got {kload.state}"
        assert heater.get_status(main_reactor.monotonic())["target"] == 0.0

        # 7e: PRUSA_KLOAD_CANCEL while heating / confirming / swap_wait_insert cools down heater
        gcode.run_script("M104 S215")
        kload.state = "HEATING_LOAD"
        gcode.run_script("PRUSA_KLOAD_CANCEL")
        assert kload.state == "IDLE"
        assert heater.get_status(main_reactor.monotonic())["target"] == 0.0

        gcode.run_script("PRUSA_KLOAD_LOAD MATERIAL=PLA")
        assert kload.state == "CONFIRM_PURGE"
        assert heater.get_status(main_reactor.monotonic())["target"] == 210.0
        gcode.run_script("PRUSA_KLOAD_CANCEL")
        assert kload.state == "IDLE"
        assert heater.get_status(main_reactor.monotonic())["target"] == 0.0

        gcode.run_script("PRUSA_KLOAD_SET_FILAMENT MATERIAL=PLA")
        gcode.run_script("PRUSA_KLOAD_SWAP MATERIAL=PETG")
        assert kload.state == "SWAP_WAIT_INSERT"
        assert heater.get_status(main_reactor.monotonic())["target"] == 210.0
        gcode.run_script("PRUSA_KLOAD_CANCEL")
        assert kload.state == "IDLE"
        assert heater.get_status(main_reactor.monotonic())["target"] == 0.0

    run_klippy_inprocess(driver)
    print(
        "PASS: Scenario 7 — Live Klipper Reactor debounced sensor, mid-print isolation, "
        "unknown unload & cancel cooldown"
    )


def test_scenario_8_live_menu_jinja2_evaluation():
    def driver(printer, main_reactor, sdcard_dir, _state_file):
        kload = printer.lookup_object("prusa_kload")
        gcode = printer.lookup_object("gcode")
        menu = printer.lookup_object("menu")

        def eval_all_kload_menus():
            menu.update_context(main_reactor.monotonic())
            results = {}
            for key, item in menu.menuitems.items():
                if key.startswith("__main __prusa_kload"):
                    results[key] = (bool(item.is_enabled()), item.render_name())
            return results

        # 8a: IDLE with no filament loaded
        res = eval_all_kload_menus()
        assert len(res) > 30, f"Expected all prusa_kload menu items, got {len(res)}"
        assert res["__main __prusa_kload"][0] is True
        assert res["__main __prusa_kload __loaded"][1] == "Loaded: None"
        assert res["__main __prusa_kload __confirm_purge"][0] is False
        assert res["__main __prusa_kload __mismatch"][0] is False
        assert res["__main __prusa_kload __pick_material"][0] is False

        # 8b: SELECT_MATERIAL state
        gcode.run_script("PRUSA_KLOAD_LOAD")
        assert kload.state == "SELECT_MATERIAL"
        res = eval_all_kload_menus()
        assert res["__main __prusa_kload __pick_material"][0] is True
        assert res["__main __prusa_kload __confirm_purge"][0] is False
        assert res["__main __prusa_kload __mismatch"][0] is False

        # 8c: CONFIRM_PURGE state
        gcode.run_script("PRUSA_KLOAD_LOAD MATERIAL=PLA")
        assert kload.state == "CONFIRM_PURGE"
        res = eval_all_kload_menus()
        assert res["__main __prusa_kload __confirm_purge"][0] is True
        assert res["__main __prusa_kload __pick_material"][0] is False
        assert res["__main __prusa_kload __mismatch"][0] is False

        # 8d: Loaded PLA -> IDLE
        gcode.run_script("PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES")
        res = eval_all_kload_menus()
        assert res["__main __prusa_kload __loaded"][1] == "Loaded: PLA"
        assert res["__main __prusa_kload __confirm_purge"][0] is False

        # 8e: MISMATCH_PROMPT state (requested=PETG)
        with open(os.path.join(sdcard_dir, "petg.gcode"), "w", encoding="utf-8") as handle:
            handle.write("; filament_type = PETG\nG28\n")
        gcode.run_script("SDCARD_PRINT_FILE FILENAME=petg.gcode")
        assert kload.state == "MISMATCH_PROMPT"
        res = eval_all_kload_menus()
        assert res["__main __prusa_kload __mismatch"][0] is True
        assert res["__main __prusa_kload __mismatch __swap"][1] == "Swap -> PETG"
        assert res["__main __prusa_kload __confirm_purge"][0] is False
        assert res["__main __prusa_kload __pick_material"][0] is False

    run_klippy_inprocess(driver)
    print("PASS: Scenario 8 — Real Klipper MenuManager Jinja2 evaluation across all states")


def main():
    if not os.path.isfile(KLIPPY_PY):
        print(f"ERROR: klippy.py not found at {KLIPPY_PY}", file=sys.stderr)
        sys.exit(2)
    if not os.path.isfile(DICT_PATH):
        print(f"ERROR: MCU dict not found at {DICT_PATH}", file=sys.stderr)
        sys.exit(2)

    test_scenario_1_boot_menus_and_commands()
    test_scenario_2_full_load_purge_and_chunked_unload()
    test_scenario_3_sdcard_intercept_force_cancel_and_swap()
    test_scenario_4_print_start_check_and_standalone_pin()
    test_scenario_5_retry_swap_cancel_and_unknown_unload()
    test_scenario_6_invalid_config_rejection()
    test_scenario_7_live_reactor_sensor_debounce_isolation_and_cancel()
    test_scenario_8_live_menu_jinja2_evaluation()
    print("\nALL REAL KLIPPER SIMULATOR INTEGRATION TESTS PASSED!")


if __name__ == "__main__":
    main()
