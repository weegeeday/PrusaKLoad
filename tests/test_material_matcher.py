from prusa_kload import MaterialRegistry


def test_builtin_presets_and_case_insensitive_match():
    reg = MaterialRegistry()
    ok, req, reason = reg.matches("pla", "PLA")
    assert ok is True
    assert req == "PLA"
    assert reason == "match"


def test_alias_and_multi_tool_resolution():
    reg = MaterialRegistry()
    ok, req, reason = reg.matches("PLA+;PETG", "PLA")
    assert ok is True
    assert req == "PLA"
    assert reason == "match"

    ok2, req2, reason2 = reg.matches("NYLON", "PA")
    assert ok2 is True
    assert req2 == "PA"
    assert reason2 == "match"


def test_no_metadata_passes_without_warning():
    reg = MaterialRegistry()
    ok, req, reason = reg.matches(None, None)
    assert ok is True
    assert req is None
    assert reason == "no_metadata"

    ok2, req2, reason2 = reg.matches(" ; ", "PLA")
    assert ok2 is True
    assert req2 is None
    assert reason2 == "no_metadata"


def test_unloaded_filament_triggers_mismatch_when_gcode_specifies_material():
    reg = MaterialRegistry()
    ok, req, reason = reg.matches("PETG", None)
    assert ok is False
    assert req == "PETG"
    assert reason == "not_loaded"


def test_mismatch_when_different_material_loaded():
    reg = MaterialRegistry()
    ok, req, reason = reg.matches("ASA", "PLA")
    assert ok is False
    assert req == "ASA"
    assert reason == "mismatch"


def test_material_section_override_and_custom_material(klipper_env):
    _, kload, _, _, _ = klipper_env(
        material_sections={
            "PLA": {"load_temp": "210", "aliases": "PLA+, MATTE PLA"},
            "CF-PA": {
                "load_temp": "285",
                "unload_temp": "280",
                "purge_distance": "40",
                "aliases": "PAHT-CF",
            },
        }
    )

    pla = kload.materials.get("PLA")
    assert pla.load_temp == 210.0
    assert pla.unload_temp == 210.0
    assert kload.materials.resolve_name("Matte PLA") == "PLA"

    cfpa = kload.materials.get("CF-PA")
    assert cfpa is not None
    assert cfpa.load_temp == 285.0
    assert cfpa.unload_temp == 280.0
    assert cfpa.purge_distance == 40.0
    assert kload.materials.resolve_name("PAHT-CF") == "CF-PA"

