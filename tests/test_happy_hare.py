import prusa_kload


class FakeHappyHareMMU:
    def __init__(self, enabled=True, gate=0):
        self.enabled = enabled
        self.gate = gate

    def get_status(self, eventtime):
        return {
            "enabled": self.enabled,
            "gate": self.gate,
        }


def test_happy_hare_active_stands_down_and_warns(klipper_env):
    printer, kload, sensor, sdcard_dir, state_file = klipper_env(connect=False)
    mmu = FakeHappyHareMMU(enabled=True, gate=0)
    printer.objects["mmu"] = mmu

    hh_calls = []
    printer.gcode.register_command("LOAD_FILAMENT", lambda gcmd: hh_calls.append("hh_load"))
    printer.gcode.register_command("UNLOAD_FILAMENT", lambda gcmd: hh_calls.append("hh_unload"))

    printer.send_event("klippy:connect")
    printer.send_event("klippy:ready")

    # Startup warning emitted in console
    assert any("Happy Hare (MMU) detected" in msg for msg in printer.gcode.info_messages)

    status = kload.get_status(100.0)
    assert status["happy_hare_detected"] is True
    assert status["happy_hare_active"] is True

    # Sensor insert is ignored (no material picker, no state written)
    sensor.runout_helper.note_filament_present(printer.reactor.monotonic(), True)
    printer.reactor.advance(1.5)
    assert kload.state == prusa_kload.STATE_IDLE
    assert not state_file.exists()

    # SDCARD_PRINT_FILE passes straight through without mismatch prompt
    (sdcard_dir / "petg.gcode").write_text("; filament_type = PETG\nG28\n", encoding="utf-8")
    printer.gcode.run_command("SDCARD_PRINT_FILE", FILENAME="petg.gcode")
    assert kload.state == prusa_kload.STATE_IDLE
    assert printer.virtual_sdcard.started_files == ["petg.gcode"]

    # LOAD_FILAMENT / UNLOAD_FILAMENT route to Happy Hare's original handlers
    printer.gcode.run_command("LOAD_FILAMENT")
    printer.gcode.run_command("UNLOAD_FILAMENT")
    assert hh_calls == ["hh_load", "hh_unload"]

    # Manual PRUSA_KLOAD_LOAD / UNLOAD / SWAP / CHECK / SET_FILAMENT / CLEAR_FILAMENT warn and stand down
    for cmd, params in (
        ("PRUSA_KLOAD_LOAD", {"MATERIAL": "PLA"}),
        ("PRUSA_KLOAD_UNLOAD", {}),
        ("PRUSA_KLOAD_SWAP", {"MATERIAL": "PETG"}),
        ("PRUSA_KLOAD_CHECK", {"MATERIAL": "PETG"}),
        ("PRUSA_KLOAD_SET_FILAMENT", {"MATERIAL": "PLA"}),
        ("PRUSA_KLOAD_CLEAR_FILAMENT", {}),
    ):
        printer.gcode.info_messages.clear()
        printer.gcode.run_command(cmd, **params)
        assert kload.state == prusa_kload.STATE_IDLE
        assert any("Happy Hare (MMU) is active" in m for m in printer.gcode.info_messages)

    # STATUS, LIST_MATERIALS, CANCEL still work
    printer.gcode.run_command("PRUSA_KLOAD_STATUS")
    assert "happy_hare=active" in printer.gcode.info_messages[-1]
    printer.gcode.run_command("PRUSA_KLOAD_LIST_MATERIALS")
    assert "PLA: load=215C" in printer.gcode.info_messages[-1]
    printer.gcode.run_command("PRUSA_KLOAD_CANCEL")
    assert "operation cancelled" in printer.gcode.info_messages[-1]


def test_happy_hare_bypass_and_disabled_activates_prusa_kload(klipper_env):
    printer, kload, sensor, sdcard_dir, state_file = klipper_env(connect=False)
    mmu = FakeHappyHareMMU(enabled=True, gate=-2)  # GATE_BYPASS
    printer.objects["mmu"] = mmu

    hh_calls = []
    printer.gcode.register_command("LOAD_FILAMENT", lambda gcmd: hh_calls.append("hh_load"))
    printer.gcode.register_command("UNLOAD_FILAMENT", lambda gcmd: hh_calls.append("hh_unload"))

    printer.send_event("klippy:connect")
    printer.send_event("klippy:ready")

    status = kload.get_status(100.0)
    assert status["happy_hare_detected"] is True
    assert status["happy_hare_active"] is False

    # Sensor insert triggers PrusaKLoad picker in bypass mode
    sensor.runout_helper.note_filament_present(printer.reactor.monotonic(), True)
    printer.reactor.advance(1.5)
    assert kload.state == prusa_kload.STATE_SELECT_MATERIAL

    # LOAD_FILAMENT routes to PrusaKLoad in bypass mode
    printer.gcode.run_command("LOAD_FILAMENT", MATERIAL="PLA")
    assert kload.state == prusa_kload.STATE_CONFIRM_PURGE
    assert hh_calls == []
    printer.gcode.run_command("PRUSA_KLOAD_CONFIRM_PURGE", ACTION="YES")
    assert kload.loaded_filament == "PLA"
    assert state_file.exists()

    # Pre-print mismatch check works in bypass mode
    (sdcard_dir / "petg.gcode").write_text("; filament_type = PETG\nG28\n", encoding="utf-8")
    printer.gcode.run_command("SDCARD_PRINT_FILE", FILENAME="petg.gcode")
    assert kload.state == prusa_kload.STATE_MISMATCH_PROMPT
    printer.gcode.run_command("PRUSA_KLOAD_MISMATCH_ACTION", ACTION="CANCEL")

    # Disabled MMU (enabled=False, gate=0) also activates PrusaKLoad
    mmu.gate = 0
    mmu.enabled = False
    assert kload.get_status(100.0)["happy_hare_active"] is False
    printer.gcode.run_command("UNLOAD_FILAMENT")
    assert kload.state == prusa_kload.STATE_WAIT_REMOVE
    assert hh_calls == []


def test_happy_hare_compat_false_disables_detection(klipper_env):
    printer, kload, _, _, _ = klipper_env(
        options={"happy_hare_compat": "False"}, connect=False
    )
    printer.objects["mmu"] = FakeHappyHareMMU(enabled=True, gate=0)
    printer.send_event("klippy:connect")
    printer.send_event("klippy:ready")

    status = kload.get_status(100.0)
    assert status["happy_hare_detected"] is False
    assert status["happy_hare_active"] is False
    printer.gcode.run_command("PRUSA_KLOAD_SET_FILAMENT", MATERIAL="PLA")
    assert kload.loaded_filament == "PLA"

