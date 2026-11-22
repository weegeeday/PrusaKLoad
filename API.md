# PrusaKLoad API Reference

PrusaKLoad exposes its state and controls through four surfaces:

1. Moonraker HTTP and WebSocket APIs (`printer.prusa_kload`)
2. Klipper G-code commands (`PRUSA_KLOAD_*`, `LOAD_FILAMENT`, `UNLOAD_FILAMENT`)
3. Klipper Jinja2 templates and Python extras
4. The on-disk JSON state file (`state_file`)

---

## 1. Status Object Schema (`printer.prusa_kload`)

Every Moonraker query, WebSocket subscription, and Klipper template reads the dictionary returned by `PrusaKLoad.get_status(eventtime)`:

| Field | Type | Example | Description |
|---|---|---|---|
| `state` | `string` | `"IDLE"` | Current state machine state (see [State Values](#state-values)). |
| `loaded_filament` | `string \| null` | `"PLA"` | Canonical uppercase name of the currently loaded filament, or `null` if unloaded/unknown. |
| `target_filament` | `string \| null` | `"PETG"` | Material actively being loaded, unloaded, or swapped. `null` when idle. |
| `requested_filament` | `string \| null` | `"PETG"` | Material requested by a checked G-code file or `PRUSA_KLOAD_CHECK`. |
| `pending_filename` | `string \| null` | `"petg_bracket.gcode"` | SD card filename waiting on a mismatch decision or swap. |
| `filament_present` | `bool \| null` | `true` | Current filament sensor state (`true` = detected, `false` = empty, `null` = unknown). |
| `materials` | `string[]` | `["PLA", "PETG", ...]` | Ordered list of all registered canonical material names (built-in + `[prusa_kload_material]` additions). |
| `happy_hare_detected` | `bool` | `false` | `true` when Klipper's `mmu` object is present and `happy_hare_compat` is enabled. |
| `happy_hare_active` | `bool` | `false` | `true` when Happy Hare is enabled (`enabled == true`) and not in bypass mode (`gate != -2`). |

### State Values

| State | Meaning |
|---|---|
| `IDLE` | No PrusaKLoad operation is running. |
| `SELECT_MATERIAL` | Waiting for the user to pick a material (load, unload, or manual set). |
| `HEATING_LOAD` | Heating the extruder to the material's `load_temp`. |
| `LOADING` | Feeding `load_distance` and `purge_distance`. |
| `CONFIRM_PURGE` | Waiting for `YES`, `PURGE_MORE`, `RETRY`, or `CANCEL`. |
| `HEATING_UNLOAD` | Heating the extruder to the material's `unload_temp`. |
| `UNLOADING` | Running ramming, cooling strokes, and retraction. |
| `WAIT_REMOVE` | Waiting for the filament sensor to clear after retraction. |
| `MISMATCH_PROMPT` | Holding a print start while waiting for `SWAP`, `FORCE`, or `CANCEL`. |
| `SWAP_WAIT_INSERT` | Waiting for the next filament to be inserted during a swap. |

---

## 2. Moonraker HTTP REST API

### Query Only the Loaded Filament

```bash
curl "http://<printer-ip>:7125/printer/objects/query?prusa_kload=loaded_filament"
```

Response:

```json
{
  "result": {
    "eventtime": 1234.56,
    "status": {
      "prusa_kload": {
        "loaded_filament": "PLA"
      }
    }
  }
}
```

### Query the Full `prusa_kload` Object

```bash
curl "http://<printer-ip>:7125/printer/objects/query?prusa_kload"
```

Response:

```json
{
  "result": {
    "eventtime": 1234.56,
    "status": {
      "prusa_kload": {
        "state": "IDLE",
        "loaded_filament": "PLA",
        "target_filament": null,
        "requested_filament": null,
        "pending_filename": null,
        "filament_present": true,
        "materials": ["PLA", "PETG", "ASA", "ABS", "PC", "TPU", "PA", "HIPS", "PVB", "FLEX"],
        "happy_hare_detected": false,
        "happy_hare_active": false
      }
    }
  }
}
```

### Run a PrusaKLoad Command over HTTP

```bash
# Set loaded filament without heating/extruding
curl -X POST "http://<printer-ip>:7125/printer/gcode/script?script=PRUSA_KLOAD_SET_FILAMENT%20MATERIAL=PETG"

# Start an unload
curl -X POST "http://<printer-ip>:7125/printer/gcode/script?script=PRUSA_KLOAD_UNLOAD"
```

---

## 3. Moonraker JSON-RPC WebSocket API

Connect to `ws://<printer-ip>:7125/websocket`.

### One-Time Query (`printer.objects.query`)

```json
{
  "jsonrpc": "2.0",
  "method": "printer.objects.query",
  "params": {
    "objects": {
      "prusa_kload": ["loaded_filament", "state", "happy_hare_active"]
    }
  },
  "id": 1
}
```

### Live Subscription (`printer.objects.subscribe`)

```json
{
  "jsonrpc": "2.0",
  "method": "printer.objects.subscribe",
  "params": {
    "objects": {
      "prusa_kload": null
    }
  },
  "id": 2
}
```

Whenever any field changes, Moonraker pushes a `notify_status_update` notification:

```json
{
  "jsonrpc": "2.0",
  "method": "notify_status_update",
  "params": [
    {
      "prusa_kload": {
        "state": "CONFIRM_PURGE",
        "target_filament": "PLA"
      }
    },
    1240.12
  ]
}
```

### Running Commands (`printer.gcode.script`)

```json
{
  "jsonrpc": "2.0",
  "method": "printer.gcode.script",
  "params": {
    "script": "PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES"
  },
  "id": 3
}
```

### Interactive Prompt Notifications (`notify_gcode_response`)

PrusaKLoad emits standard Klipper `action:prompt_*` lines over `gcode.respond_raw(...)`, which Moonraker delivers as `notify_gcode_response` frames:

- `// action:prompt_begin <title>`
- `// action:prompt_text <message>`
- `// action:prompt_button_group_start` / `// action:prompt_button <label>|<command>|<style>` / `// action:prompt_button_group_end`
- `// action:prompt_footer_button <label>|<command>|<style>`
- `// action:prompt_show`
- `// action:prompt_end`

---

## 4. G-Code Command API

### Core Commands

| Command | Parameters | Console Output / Effect |
|---|---|---|
| `PRUSA_KLOAD_STATUS` | None | Responds with `PrusaKLoad state=<S> loaded=<L> target=<T> requested=<R> sensor=<B> happy_hare=<active\|bypass\|none>`. |
| `PRUSA_KLOAD_GET_FILAMENT` | None | Responds with `Loaded filament: <NAME or None>`. |
| `PRUSA_KLOAD_SET_FILAMENT` | `MATERIAL=<name>` (optional) | Resolves aliases, saves `loaded_filament`, and updates state without heating. Opens the material picker if `MATERIAL` is omitted. |
| `PRUSA_KLOAD_CLEAR_FILAMENT` | None | Sets `loaded_filament` to `None` and persists state. |
| `PRUSA_KLOAD_LIST_MATERIALS` | None | Responds with one line per material: `<NAME>: load=<T>C unload=<T>C aliases=[...]`. |
| `PRUSA_KLOAD_LOAD` | `MATERIAL=<name>` (optional) | Heats to `load_temp`, loads and purges, then enters `CONFIRM_PURGE`. Opens the material picker if `MATERIAL` is omitted. |
| `PRUSA_KLOAD_UNLOAD` | `MATERIAL=<name>` (optional), `CONFIRM=1` (optional) | Heats to `unload_temp`, rams/retracts, and waits for sensor release (`WAIT_REMOVE`). Passing `CONFIRM=1` finalizes `WAIT_REMOVE` manually (safe no-op in other states). |
| `PRUSA_KLOAD_SWAP` | `MATERIAL=<name>` (optional) | Unloads current filament if present, enters `SWAP_WAIT_INSERT`, and loads `MATERIAL` once inserted. |
| `PRUSA_KLOAD_CONFIRM_PURGE` | `ACTION=YES\|PURGE_MORE\|RETRY` | Answers `CONFIRM_PURGE`. `YES` saves material, runs `after_load_gcode`, and either starts/resumes a pending print (skipping cooldown) or cools down. |
| `PRUSA_KLOAD_MISMATCH_ACTION` | `ACTION=SWAP\|FORCE\|CANCEL` | Answers `MISMATCH_PROMPT`. |
| `PRUSA_KLOAD_CHECK` | `MATERIAL=<name>` | Checks `MATERIAL` against `loaded_filament`. Pauses (`PAUSE`) and opens `MISMATCH_PROMPT` if mismatched while printing. |
| `PRUSA_KLOAD_CANCEL` | None | Cancels any active flow, closes prompts, cools down (`M104 S0`) if heated, calls `CANCEL_PRINT` if paused by `PRUSA_KLOAD_CHECK`, and returns to `IDLE`. |

### Wrapped / Overridden Commands

- **`LOAD_FILAMENT` / `UNLOAD_FILAMENT`**: When `override_load_unload: True` (default), PrusaKLoad wraps these commands on `klippy:ready`. They run `cmd_LOAD` / `cmd_UNLOAD`, or pass straight through to Happy Hare's original handlers while `happy_hare_active` is `True`.
- **`SDCARD_PRINT_FILE`**: When `check_on_print_start: True` (default) and Happy Hare is not active, PrusaKLoad checks the file's G-code header/footer before delegating to `virtual_sdcard`.

### Behavior While Happy Hare Is Active

When `happy_hare_active` is `True`, `PRUSA_KLOAD_LOAD`, `PRUSA_KLOAD_UNLOAD`, `PRUSA_KLOAD_SWAP`, `PRUSA_KLOAD_CHECK`, `PRUSA_KLOAD_SET_FILAMENT`, `PRUSA_KLOAD_CLEAR_FILAMENT`, `PRUSA_KLOAD_CONFIRM_PURGE`, and `PRUSA_KLOAD_MISMATCH_ACTION` do not run and instead emit a console notice. `PRUSA_KLOAD_STATUS`, `PRUSA_KLOAD_GET_FILAMENT`, `PRUSA_KLOAD_LIST_MATERIALS`, and `PRUSA_KLOAD_CANCEL` remain available.

---

## 5. Klipper Jinja2 Macros & Menu Templates

Access status fields directly in `[gcode_macro]` or `[menu]` templates:

```ini
[gcode_macro CHECK_LOADED]
gcode:
    {% set mat = printer.prusa_kload.loaded_filament or "None" %}
    {% set hh = printer.prusa_kload.happy_hare_active %}
    RESPOND MSG="Loaded: {mat} (Happy Hare active: {hh})"
```

---

## 6. Klipper Python Extra API

Other Klipper extras can look up the `PrusaKLoad` instance or import its helper utilities from `klippy/extras/prusa_kload.py`:

```python
kload = printer.lookup_object("prusa_kload", None)
if kload is not None:
    status = kload.get_status(reactor.monotonic())
    loaded = status["loaded_filament"]
    profile = kload.materials.get("PLA+")  # resolves alias -> MaterialProfile("PLA")
    is_match, canonical, reason = kload.materials.matches("PETG-CF;PLA", loaded)
```

Exported helper functions and classes in `prusa_kload.py`:

- `parse_gcode_filament(filepath, chunk_size=65536) -> str | None`: Reads the first and last `chunk_size` bytes of a G-code file and returns the first tool's material token or `None`.
- `extract_gcode_filament_metadata(filepath, chunk_size=65536) -> tuple[str | None, str | None]`: Returns `(raw_value, first_tool_material)`.
- `extract_first_tool_material(raw_value) -> str | None`: Splits `;` / `,` multi-tool strings and returns the first non-empty stripped token.
- `MaterialRegistry`: Manages presets, overrides, and alias resolution (`register_material`, `resolve_name`, `get`, `list_names`, `matches`).
- `MaterialProfile`: Holds `name`, `load_temp`, `unload_temp`, `purge_distance`, `load_speed`, `aliases`, and `to_dict()`.
- `StateStore(path)`: Atomic JSON persistence (`load()`, `save(loaded_filament)`).

---

## 7. On-Disk JSON State File

Default path: `~/printer_data/config/.prusa_kload_state.json` (configurable via `state_file`).

```json
{
  "schema_version": 1,
  "loaded_filament": "PLA",
  "last_updated": "2026-11-22T17:15:00Z"
}
```

- `schema_version`: Integer schema version (currently `1`).
- `loaded_filament`: Canonical uppercase material string or `null`.
- `last_updated`: UTC ISO-8601 timestamp (`YYYY-MM-DDTHH:MM:SSZ`).
- Writes are atomic (`<state_file>.tmp` -> `flush()` + `os.fsync()` -> `os.replace()`), so external readers never see a partially written file.

