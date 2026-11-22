import pytest
from conftest import ConfigError, GCodeError
import prusa_kload


def test_config_sensor_validation(klipper_env):
    with pytest.raises(ConfigError):
        klipper_env(sensor_name=None, standalone_pin=None, connect=False)

    with pytest.raises(ConfigError):
        klipper_env(
            options={"switch_pin": "^!PC15"},
            sensor_name="filament_switch_sensor fsensor",
            connect=False,
        )


def test_standalone_switch_pin_insert_and_load_flow(klipper_env):
    printer, kload, _, _, _ = klipper_env(
        sensor_name=None,
        standalone_pin="^!PC15",
        options={"debounce_delay": "0.5"},
    )

    # Brief chatter bounces back to False -> should NOT trigger prompt
    printer.buttons.trigger(100.0, 1)
    printer.reactor.advance(0.2)
    printer.buttons.trigger(100.2, 0)
    printer.reactor.advance(1.0)
    assert kload.state == prusa_kload.STATE_IDLE

    # Stable insert -> triggers SELECT_MATERIAL after debounce
    printer.buttons.trigger(101.2, 1)
    printer.reactor.advance(0.4)
    assert kload.state == prusa_kload.STATE_IDLE
    printer.reactor.advance(0.2)
    assert kload.state == prusa_kload.STATE_SELECT_MATERIAL
    assert any("action:prompt_begin PrusaKLoad: Select Filament" in m for m in printer.gcode.raw_messages)

    # Pick PLA -> heats, loads, purges, enters CONFIRM_PURGE
    printer.gcode.run_command("PRUSA_KLOAD_LOAD", MATERIAL="PLA")
    assert kload.state == prusa_kload.STATE_CONFIRM_PURGE
    assert "M109 S215.0" in printer.gcode.scripts
    assert any("G1 E50.000 F900.0" in s for s in printer.gcode.scripts)
    assert any("G1 E30.000 F240.0" in s for s in printer.gcode.scripts)
    assert printer.gcode_move.absolute_extrude is True

    # Purge More -> extrudes purge_more_distance and stays in CONFIRM_PURGE
    printer.gcode.run_command("PRUSA_KLOAD_CONFIRM_PURGE", ACTION="PURGE_MORE")
    assert kload.state == prusa_kload.STATE_CONFIRM_PURGE
    assert any("G1 E20.000 F240.0" in s for s in printer.gcode.scripts)

    # Retry -> re-heats/loads/purges and stays in CONFIRM_PURGE
    prev_len = len(printer.gcode.scripts)
    printer.gcode.run_command("PRUSA_KLOAD_CONFIRM_PURGE", ACTION="RETRY")
    assert kload.state == prusa_kload.STATE_CONFIRM_PURGE
    assert any("M109 S215.0" in s for s in printer.gcode.scripts[prev_len:])

    # Confirm Yes -> saves PLA, cools down, returns to IDLE
    printer.gcode.run_command("PRUSA_KLOAD_CONFIRM_PURGE", ACTION="YES")
    assert kload.state == prusa_kload.STATE_IDLE
    assert kload.loaded_filament == "PLA"
    assert kload.state_store.load() == "PLA"
    assert "M104 S0" in printer.gcode.scripts


def test_idle_insert_ignored_when_already_loaded_or_disabled(klipper_env):
    printer, kload, sensor, _, _ = klipper_env(options={"auto_load_on_insert": "False"})
    sensor.runout_helper.note_filament_present(100.0, True)
    printer.reactor.advance(1.5)
    assert kload.state == prusa_kload.STATE_IDLE
    # Native runout_helper still received the call
    assert sensor.runout_helper.native_calls[-1] == (100.0, True)

    printer2, kload2, sensor2, _, _ = klipper_env()
    printer2.gcode.run_command("PRUSA_KLOAD_SET_FILAMENT", MATERIAL="PETG")
    sensor2.runout_helper.note_filament_present(100.0, True)
    printer2.reactor.advance(1.5)
    assert kload2.state == prusa_kload.STATE_IDLE


def test_idle_remove_auto_clears_and_printing_ignores_edges(klipper_env):
    printer, kload, sensor, _, _ = klipper_env(initial_present=True)
    printer.gcode.run_command("PRUSA_KLOAD_SET_FILAMENT", MATERIAL="PLA")
    assert kload.loaded_filament == "PLA"

    # While printing, sensor changes are ignored by PrusaKLoad
    printer.print_stats.state = "printing"
    sensor.runout_helper.note_filament_present(printer.reactor.monotonic(), False)
    printer.reactor.advance(1.5)
    assert kload.loaded_filament == "PLA"
    assert kload.state == prusa_kload.STATE_IDLE

    # While idle, removal clears loaded_filament
    printer.print_stats.state = "standby"
    sensor.runout_helper.note_filament_present(printer.reactor.monotonic(), True)
    printer.reactor.advance(1.5)
    sensor.runout_helper.note_filament_present(printer.reactor.monotonic(), False)
    printer.reactor.advance(1.5)
    assert kload.loaded_filament is None
    assert kload.state_store.load() is None


def test_unload_known_and_unknown_material_with_sensor_release(klipper_env):
    printer, kload, sensor, _, _ = klipper_env(
        initial_present=True,
        options={"cooling_moves": "2", "ramming_distance": "8.0"},
    )
    printer.gcode.run_command("PRUSA_KLOAD_SET_FILAMENT", MATERIAL="ASA")
    printer.gcode.run_command("PRUSA_KLOAD_UNLOAD")

    assert kload.state == prusa_kload.STATE_WAIT_REMOVE
    assert "M109 S260.0" in printer.gcode.scripts
    assert any("G1 E8.000 F900.0" in s for s in printer.gcode.scripts)
    assert any("G1 E-80.000 F1200.0" in s for s in printer.gcode.scripts)

    # Pull filament out -> finalizes unload and cools down
    sensor.runout_helper.note_filament_present(100.0, False)
    printer.reactor.advance(1.5)
    assert kload.state == prusa_kload.STATE_IDLE
    assert kload.loaded_filament is None
    assert "M104 S0" in printer.gcode.scripts

    # Unload when loaded_filament is unknown prompts for material
    sensor.runout_helper.note_filament_present(102.0, True)
    printer.reactor.advance(1.5)
    # Close SELECT_MATERIAL from insert or clear state first
    printer.gcode.run_command("PRUSA_KLOAD_CANCEL")
    printer.gcode.run_command("PRUSA_KLOAD_UNLOAD")
    assert kload.state == prusa_kload.STATE_SELECT_MATERIAL
    printer.gcode.run_command("PRUSA_KLOAD_LOAD", MATERIAL="PETG")
    assert kload.state == prusa_kload.STATE_WAIT_REMOVE
    assert "M109 S230.0" in printer.gcode.scripts


def test_prompt_timeout_cancels_and_cools_down(klipper_env):
    printer, kload, _, _, _ = klipper_env(options={"prompt_timeout": "30.0"})
    printer.gcode.run_command("PRUSA_KLOAD_LOAD", MATERIAL="PLA")
    assert kload.state == prusa_kload.STATE_CONFIRM_PURGE

    printer.reactor.advance(31.0)
    assert kload.state == prusa_kload.STATE_IDLE
    assert "M104 S0" in printer.gcode.scripts
    assert printer.gcode.raw_messages[-1] == "// action:prompt_end"


def test_unhomed_vs_homed_parking_and_hooks(klipper_env):
    printer, kload, _, _, _ = klipper_env(
        options={
            "park_x": "15.0",
            "park_y": "25.0",
            "park_z": "40.0",
            "auto_home_on_load": "False",
            "before_load_gcode": "RESPOND MSG=before_load",
            "after_load_gcode": "RESPOND MSG=after_load",
        }
    )

    # Unhomed -> stays in place (no G28 or X/Y/Z G1 moves)
    printer.gcode.run_command("PRUSA_KLOAD_LOAD", MATERIAL="PLA")
    assert not any("G28" in s for s in printer.gcode.scripts)
    assert not any("X15.000" in s for s in printer.gcode.scripts)
    assert "RESPOND MSG=before_load" in printer.gcode.scripts

    printer.gcode.run_command("PRUSA_KLOAD_CONFIRM_PURGE", ACTION="YES")
    assert "RESPOND MSG=after_load" in printer.gcode.scripts

    # Homed -> moves to park Z then XY
    printer.toolhead.homed_axes = "xyz"
    printer.gcode.scripts.clear()
    printer.gcode.run_command("PRUSA_KLOAD_LOAD", MATERIAL="PLA")
    assert any("G1 Z40.000 F6000.0" in s for s in printer.gcode.scripts)
    assert any("G1 X15.000 Y25.000 F6000.0" in s for s in printer.gcode.scripts)


def test_manual_set_clear_status_and_extrude_temp_guard(klipper_env):
    printer, kload, _, _, _ = klipper_env(
        material_sections={"LOWTEMP": {"load_temp": "100"}}
    )
    printer.gcode.run_command("PRUSA_KLOAD_SET_FILAMENT", MATERIAL="pla+")
    assert kload.loaded_filament == "PLA"
    printer.gcode.run_command("PRUSA_KLOAD_GET_FILAMENT")
    assert "Loaded filament: PLA" in printer.gcode.info_messages[-1]

    printer.gcode.run_command("PRUSA_KLOAD_STATUS")
    assert "loaded=PLA" in printer.gcode.info_messages[-1]

    printer.gcode.run_command("PRUSA_KLOAD_LIST_MATERIALS")
    assert "PLA: load=215C" in printer.gcode.info_messages[-1]

    status = kload.get_status(100.0)
    assert status["loaded_filament"] == "PLA"
    assert status["state"] == prusa_kload.STATE_IDLE

    printer.gcode.run_command("PRUSA_KLOAD_CLEAR_FILAMENT")
    assert kload.loaded_filament is None

    with pytest.raises(GCodeError):
        printer.gcode.run_command("PRUSA_KLOAD_LOAD", MATERIAL="LOWTEMP")
    assert kload.state == prusa_kload.STATE_IDLE


def test_chunked_extrusion_respects_max_e_dist_and_confirm_idempotency(klipper_env):
    printer, kload, _, _, _ = klipper_env(
        options={"load_distance": "65.0", "unload_distance": "85.0", "ramming_distance": "0.0", "cooling_moves": "0"}
    )
    printer.extruder.max_e_dist = 50.0

    printer.gcode.run_command("PRUSA_KLOAD_LOAD", MATERIAL="PLA")
    assert "G1 E50.000 F900.0" in printer.gcode.scripts
    assert "G1 E15.000 F900.0" in printer.gcode.scripts
    printer.gcode.run_command("PRUSA_KLOAD_CONFIRM_PURGE", ACTION="YES")

    printer.gcode.scripts.clear()
    printer.gcode.run_command("PRUSA_KLOAD_UNLOAD")
    assert "G1 E-50.000 F1200.0" in printer.gcode.scripts
    assert "G1 E-35.000 F1200.0" in printer.gcode.scripts
    assert kload.state == prusa_kload.STATE_IDLE

    # Calling CONFIRM=1 when not in WAIT_REMOVE is a safe no-op
    printer.gcode.run_command("PRUSA_KLOAD_UNLOAD", CONFIRM="1")
    assert kload.state == prusa_kload.STATE_IDLE


def test_cancel_cools_down_from_hot_states_including_swap_wait_insert(klipper_env):
    printer, kload, _, _, _ = klipper_env(initial_present=False)
    printer.gcode.run_command("PRUSA_KLOAD_SET_FILAMENT", MATERIAL="PLA")

    # Start a swap -> unloads PLA (no cooldown) -> enters SWAP_WAIT_INSERT with hot nozzle
    printer.gcode.run_command("PRUSA_KLOAD_SWAP", MATERIAL="PETG")
    assert kload.state == prusa_kload.STATE_SWAP_WAIT_INSERT
    assert "M104 S0" not in printer.gcode.scripts

    # Cancelling during SWAP_WAIT_INSERT must cool down the hot nozzle
    printer.gcode.run_command("PRUSA_KLOAD_CANCEL")
    assert kload.state == prusa_kload.STATE_IDLE
    assert "M104 S0" in printer.gcode.scripts

    # Cancelling during CONFIRM_PURGE also cools down
    printer.gcode.scripts.clear()
    printer.gcode.run_command("PRUSA_KLOAD_LOAD", MATERIAL="PLA")
    assert kload.state == prusa_kload.STATE_CONFIRM_PURGE
    printer.gcode.run_command("PRUSA_KLOAD_CANCEL")
    assert kload.state == prusa_kload.STATE_IDLE
    assert "M104 S0" in printer.gcode.scripts


def test_override_load_unload_replaces_existing_macros_and_respects_toggle(klipper_env):
    # Default (override_load_unload: True) overrides pre-existing LOAD_FILAMENT / UNLOAD_FILAMENT
    printer, kload, _, _, _ = klipper_env(connect=False)
    legacy_calls = []
    printer.gcode.register_command("LOAD_FILAMENT", lambda gcmd: legacy_calls.append("load"))
    printer.gcode.register_command("UNLOAD_FILAMENT", lambda gcmd: legacy_calls.append("unload"))
    printer.send_event("klippy:connect")
    printer.send_event("klippy:ready")

    printer.gcode.run_command("LOAD_FILAMENT", MATERIAL="PLA")
    assert kload.state == prusa_kload.STATE_CONFIRM_PURGE
    assert legacy_calls == []
    printer.gcode.run_command("PRUSA_KLOAD_CONFIRM_PURGE", ACTION="YES")
    assert kload.loaded_filament == "PLA"

    printer.gcode.run_command("UNLOAD_FILAMENT")
    assert kload.loaded_filament is None
    assert legacy_calls == []

    # When override_load_unload: False, existing user macros remain intact
    printer2, kload2, _, _, _ = klipper_env(
        options={"override_load_unload": "False"}, connect=False
    )
    user_calls = []
    printer2.gcode.register_command("LOAD_FILAMENT", lambda gcmd: user_calls.append("load"))
    printer2.gcode.register_command("UNLOAD_FILAMENT", lambda gcmd: user_calls.append("unload"))
    printer2.send_event("klippy:connect")
    printer2.send_event("klippy:ready")

    printer2.gcode.run_command("LOAD_FILAMENT", MATERIAL="PLA")
    printer2.gcode.run_command("UNLOAD_FILAMENT")
    assert user_calls == ["load", "unload"]
    assert kload2.state == prusa_kload.STATE_IDLE

