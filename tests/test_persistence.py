import json
from prusa_kload import StateStore


def test_missing_file_initializes_none(tmp_path):
    store = StateStore(str(tmp_path / "does_not_exist.json"))
    assert store.load() is None


def test_atomic_save_and_reload_round_trip(tmp_path):
    path = tmp_path / "nested" / "state.json"
    store = StateStore(str(path))
    store.save("PETG")

    assert path.exists()
    assert not (tmp_path / "nested" / "state.json.tmp").exists()
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["schema_version"] == 1
    assert data["loaded_filament"] == "PETG"
    assert "last_updated" in data

    reloaded = StateStore(str(path)).load()
    assert reloaded == "PETG"

    store.save(None)
    assert StateStore(str(path)).load() is None


def test_corrupt_json_falls_back_cleanly(tmp_path, caplog):
    path = tmp_path / "corrupt.json"
    path.write_text("{broken json!!!", encoding="utf-8")
    store = StateStore(str(path))
    assert store.load() is None
    assert "ignoring unreadable state file" in caplog.text

    path.write_text('["not", "a", "dict"]', encoding="utf-8")
    assert store.load() is None

