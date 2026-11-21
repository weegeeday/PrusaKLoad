import prusa_kload


def _write_gcode(sdcard_dir, name, material_line):
    path = sdcard_dir / name
    content = "%s\nG28\nG1 X10 Y10\n" % (material_line,) if material_line else "G28\n"
    path.write_text(content, encoding="utf-8")
    return name


def test_sdcard_match_and_no_metadata_pass_through(klipper_env):
    printer, kload, _, sdcard_dir, _ = klipper_env()
    printer.gcode.run_command("PRUSA_KLOAD_SET_FILAMENT", MATERIAL="PLA")

    _write_gcode(sdcard_dir, "pla_part.gcode", "; filament_type = PLA+")
    printer.gcode.run_command("SDCARD_PRINT_FILE", FILENAME="pla_part.gcode")
    assert kload.state == prusa_kload.STATE_IDLE
    assert printer.virtual_sdcard.started_files == ["pla_part.gcode"]

    _write_gcode(sdcard_dir, "plain.gcode", None)
    printer.gcode.run_command("SDCARD_PRINT_FILE", FILENAME="plain.gcode")
    assert printer.virtual_sdcard.started_files == ["pla_part.gcode", "plain.gcode"]


def test_sdcard_mismatch_force_continue_and_cancel(klipper_env):
    printer, kload, _, sdcard_dir, _ = klipper_env()
    printer.gcode.run_command("PRUSA_KLOAD_SET_FILAMENT", MATERIAL="PLA")
    _write_gcode(sdcard_dir, "petg_part.gcode", "; filament_type = PETG")

    # Mismatch stops before starting print
    printer.gcode.run_command("SDCARD_PRINT_FILE", FILENAME="petg_part.gcode")
    assert kload.state == prusa_kload.STATE_MISMATCH_PROMPT
    assert kload.requested_filament == "PETG"
    assert printer.virtual_sdcard.started_files == []

    # Cancel discards print
    printer.gcode.run_command("PRUSA_KLOAD_MISMATCH_ACTION", ACTION="CANCEL")
    assert kload.state == prusa_kload.STATE_IDLE
    assert printer.virtual_sdcard.started_files == []

    # Trigger again -> Force Continue starts without recursion loop
    printer.gcode.run_command("SDCARD_PRINT_FILE", FILENAME="petg_part.gcode")
    assert kload.state == prusa_kload.STATE_MISMATCH_PROMPT
    printer.gcode.run_command("PRUSA_KLOAD_MISMATCH_ACTION", ACTION="FORCE")
    assert kload.state == prusa_kload.STATE_IDLE
    assert printer.virtual_sdcard.started_files == ["petg_part.gcode"]


def test_sdcard_mismatch_full_swap_flow_skips_cooldown_and_starts_print(klipper_env):
    printer, kload, sensor, sdcard_dir, _ = klipper_env(initial_present=True)
    printer.gcode.run_command("PRUSA_KLOAD_SET_FILAMENT", MATERIAL="PLA")
    _write_gcode(sdcard_dir, "petg_part.gcode", "; filament_type = PETG")

    printer.gcode.run_command("SDCARD_PRINT_FILE", FILENAME="petg_part.gcode")
    assert kload.state == prusa_kload.STATE_MISMATCH_PROMPT

    # Choose Swap -> heats PLA unload temp, retracts, waits for sensor release
    printer.gcode.run_command("PRUSA_KLOAD_MISMATCH_ACTION", ACTION="SWAP")
    assert kload.state == prusa_kload.STATE_WAIT_REMOVE
    assert "M109 S215.0" in printer.gcode.scripts

    # Pull old PLA out -> transitions to SWAP_WAIT_INSERT (no cooldown!)
    printer.gcode.scripts.clear()
    sensor.runout_helper.note_filament_present(100.0, False)
    printer.reactor.advance(1.5)
    assert kload.state == prusa_kload.STATE_SWAP_WAIT_INSERT
    assert kload.loaded_filament is None
    assert "M104 S0" not in printer.gcode.scripts

    # Insert new PETG -> auto-heats PETG load temp, loads, enters CONFIRM_PURGE
    sensor.runout_helper.note_filament_present(102.0, True)
    printer.reactor.advance(1.5)
    assert kload.state == prusa_kload.STATE_CONFIRM_PURGE
    assert "M109 S230.0" in printer.gcode.scripts

    # Confirm purge YES -> saves PETG, skips cooldown, auto-starts pending print
    printer.gcode.scripts.clear()
    printer.gcode.run_command("PRUSA_KLOAD_CONFIRM_PURGE", ACTION="YES")
    assert kload.state == prusa_kload.STATE_IDLE
    assert kload.loaded_filament == "PETG"
    assert "M104 S0" not in printer.gcode.scripts
    assert printer.virtual_sdcard.started_files == ["petg_part.gcode"]


def test_sdcard_mismatch_swap_when_no_filament_loaded(klipper_env):
    printer, kload, sensor, sdcard_dir, _ = klipper_env(initial_present=False)
    _write_gcode(sdcard_dir, "asa_part.gcode", "; filament_type = ASA")

    printer.gcode.run_command("SDCARD_PRINT_FILE", FILENAME="asa_part.gcode")
    assert kload.state == prusa_kload.STATE_MISMATCH_PROMPT

    printer.gcode.run_command("PRUSA_KLOAD_MISMATCH_ACTION", ACTION="SWAP")
    assert kload.state == prusa_kload.STATE_SWAP_WAIT_INSERT

    sensor.runout_helper.note_filament_present(100.0, True)
    printer.reactor.advance(1.5)
    assert kload.state == prusa_kload.STATE_CONFIRM_PURGE
    assert "M109 S260.0" in printer.gcode.scripts

    printer.gcode.run_command("PRUSA_KLOAD_CONFIRM_PURGE", ACTION="YES")
    assert kload.loaded_filament == "ASA"
    assert printer.virtual_sdcard.started_files == ["asa_part.gcode"]


def test_print_start_check_pauses_and_resumes_or_cancels(klipper_env):
    printer, kload, sensor, _, _ = klipper_env(initial_present=True)
    printer.gcode.run_command("PRUSA_KLOAD_SET_FILAMENT", MATERIAL="PLA")

    # Simulate active PRINT_START while printing
    printer.print_stats.state = "printing"
    printer.gcode.run_command("PRUSA_KLOAD_CHECK", MATERIAL="PETG")
    assert printer.pause_calls == 1
    assert printer.print_stats.state == "paused"
    assert kload.state == prusa_kload.STATE_MISMATCH_PROMPT

    # Swap during paused PRINT_START -> completes and calls RESUME
    printer.gcode.run_command("PRUSA_KLOAD_MISMATCH_ACTION", ACTION="SWAP")
    assert kload.state == prusa_kload.STATE_WAIT_REMOVE
    sensor.runout_helper.note_filament_present(100.0, False)
    printer.reactor.advance(1.5)
    assert kload.state == prusa_kload.STATE_SWAP_WAIT_INSERT

    sensor.runout_helper.note_filament_present(102.0, True)
    printer.reactor.advance(1.5)
    assert kload.state == prusa_kload.STATE_CONFIRM_PURGE
    printer.gcode.run_command("PRUSA_KLOAD_CONFIRM_PURGE", ACTION="YES")
    assert kload.loaded_filament == "PETG"
    assert printer.resume_calls == 1
    assert printer.print_stats.state == "printing"

    # Mismatch cancel during PRINT_START calls CANCEL_PRINT
    printer.gcode.run_command("PRUSA_KLOAD_CHECK", MATERIAL="ABS")
    assert printer.pause_calls == 2
    printer.gcode.run_command("PRUSA_KLOAD_MISMATCH_ACTION", ACTION="CANCEL")
    assert printer.cancel_calls == 1
    assert printer.print_stats.state == "cancelled"

