# PrusaKLoad - Prusa MK4S-style filament loading, tracking, and mismatch checks
#
# Copyright (c) 2026 weegeeday
#
# This file may be distributed under the terms of the MIT license.

from datetime import datetime, timezone
import json
import logging
import os
import re

STATE_IDLE = "IDLE"
STATE_SELECT_MATERIAL = "SELECT_MATERIAL"
STATE_HEATING_LOAD = "HEATING_LOAD"
STATE_LOADING = "LOADING"
STATE_CONFIRM_PURGE = "CONFIRM_PURGE"
STATE_HEATING_UNLOAD = "HEATING_UNLOAD"
STATE_UNLOADING = "UNLOADING"
STATE_WAIT_REMOVE = "WAIT_REMOVE"
STATE_MISMATCH_PROMPT = "MISMATCH_PROMPT"
STATE_SWAP_WAIT_INSERT = "SWAP_WAIT_INSERT"

DEFAULT_MATERIALS = (
    ("PLA", 215.0, 215.0, ("PLA+", "PLA-CF", "SILK PLA")),
    ("PETG", 230.0, 230.0, ("PETG-CF", "PET")),
    ("ASA", 260.0, 260.0, ()),
    ("ABS", 255.0, 255.0, ("ABS+",)),
    ("PC", 275.0, 275.0, ("PCCF", "PC-CF")),
    ("TPU", 230.0, 230.0, ("TPU95A",)),
    ("PA", 275.0, 275.0, ("NYLON", "PA12", "PA6", "PA-CF")),
    ("HIPS", 220.0, 220.0, ()),
    ("PVB", 215.0, 215.0, ()),
    ("FLEX", 240.0, 240.0, ("TPE",)),
)

_PRIMARY_FILAMENT_HEADER_RE = re.compile(
    r"^\s*;\s*(?:filament_type|material_type|material\.name)\s*[:=]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_FALLBACK_FILAMENT_HEADER_RE = re.compile(
    r"^\s*;\s*material\s*[:=]\s*(.+?)\s*$",
    re.IGNORECASE,
)
_NUMERIC_ONLY_RE = re.compile(r"^\d+(?:\.\d+)?$")


class MaterialProfile:
    """Thermal, kinematic, and alias metadata for a filament material."""

    def __init__(
        self,
        name,
        load_temp,
        unload_temp=None,
        purge_distance=None,
        load_speed=None,
        aliases=(),
    ):
        self.name = str(name).strip().upper()
        self.load_temp = float(load_temp)
        self.unload_temp = float(load_temp if unload_temp is None else unload_temp)
        self.purge_distance = None if purge_distance is None else float(purge_distance)
        self.load_speed = None if load_speed is None else float(load_speed)
        cleaned = []
        seen = set()
        for alias in aliases:
            token = str(alias).strip().upper()
            if token and token != self.name and token not in seen:
                cleaned.append(token)
                seen.add(token)
        self.aliases = tuple(cleaned)

    def to_dict(self):
        return {
            "name": self.name,
            "load_temp": self.load_temp,
            "unload_temp": self.unload_temp,
            "purge_distance": self.purge_distance,
            "load_speed": self.load_speed,
            "aliases": list(self.aliases),
        }


def extract_first_tool_material(raw_value):
    """Extract and clean the first tool's material token from slicer metadata."""
    if raw_value is None:
        return None
    parts = re.split(r"[;,]", str(raw_value))
    for part in parts:
        cleaned = part.strip().strip("\"'").strip()
        if cleaned:
            return cleaned
    return None


class MaterialRegistry:
    """Registry of built-in and user-configured filament material profiles."""

    def __init__(self):
        self.materials = {}
        self._alias_map = {}
        for name, load_temp, unload_temp, aliases in DEFAULT_MATERIALS:
            self.register_material(
                name=name,
                load_temp=load_temp,
                unload_temp=unload_temp,
                aliases=aliases,
            )

    def register_material(
        self,
        name,
        load_temp,
        unload_temp=None,
        purge_distance=None,
        load_speed=None,
        aliases=(),
    ):
        canonical = str(name).strip().upper()
        if not canonical:
            raise ValueError("Material name cannot be empty")
        stale = [k for k, v in self._alias_map.items() if v == canonical]
        for key in stale:
            del self._alias_map[key]

        profile = MaterialProfile(
            name=canonical,
            load_temp=load_temp,
            unload_temp=unload_temp,
            purge_distance=purge_distance,
            load_speed=load_speed,
            aliases=aliases,
        )
        self.materials[canonical] = profile
        for alias in profile.aliases:
            self._alias_map[alias] = canonical
        return profile

    def resolve_name(self, raw):
        """Normalize a raw or multi-tool material string via case-folding and aliases."""
        token = extract_first_tool_material(raw)
        if not token:
            return None
        upper = token.upper()
        if upper in self.materials:
            return upper
        if upper in self._alias_map:
            return self._alias_map[upper]
        return upper

    def get(self, name):
        canonical = self.resolve_name(name)
        if not canonical:
            return None
        return self.materials.get(canonical)

    def list_names(self):
        return list(self.materials.keys())

    def matches(self, requested_raw, loaded):
        """Compare requested material against loaded material.

        Returns ``(is_match, canonical_requested, reason)`` where ``reason`` is one
        of ``'no_metadata'``, ``'not_loaded'``, ``'match'``, or ``'mismatch'``.
        """
        canonical_req = self.resolve_name(requested_raw)
        if not canonical_req:
            return True, None, "no_metadata"
        canonical_loaded = self.resolve_name(loaded) if loaded else None
        if not canonical_loaded:
            return False, canonical_req, "not_loaded"
        if canonical_req == canonical_loaded:
            return True, canonical_req, "match"
        return False, canonical_req, "mismatch"


def extract_gcode_filament_metadata(filepath, chunk_size=131072):
    """Read head and tail chunks of a G-code file to find slicer filament metadata.

    Explicit material keys (``filament_type``, ``material_type``, ``material.name``)
    are checked across both head and tail before generic ``material`` fallbacks, and
    purely numeric values (such as slot counts/IDs) are ignored.

    Returns ``(raw_value, first_tool_material)`` or ``(None, None)``.
    """
    if not filepath or not os.path.isfile(filepath):
        return None, None
    try:
        with open(filepath, "rb") as handle:
            head_bytes = handle.read(chunk_size)
            handle.seek(0, os.SEEK_END)
            file_size = handle.tell()
            tail_bytes = b""
            if file_size > chunk_size:
                tail_offset = max(chunk_size, file_size - chunk_size)
                handle.seek(tail_offset, os.SEEK_SET)
                tail_bytes = handle.read(chunk_size)
    except OSError as exc:
        logging.warning("prusa_kload: failed to read gcode '%s': %s", filepath, exc)
        return None, None

    chunks = [
        chunk.decode("utf-8", errors="ignore").splitlines()
        for chunk in (head_bytes, tail_bytes)
        if chunk
    ]
    for pattern in (_PRIMARY_FILAMENT_HEADER_RE, _FALLBACK_FILAMENT_HEADER_RE):
        for lines in chunks:
            for line in lines:
                match = pattern.match(line)
                if not match:
                    continue
                raw_val = match.group(1).strip()
                first = extract_first_tool_material(raw_val)
                if first and not _NUMERIC_ONLY_RE.match(first):
                    return raw_val, first
    return None, None


def parse_gcode_filament(filepath, chunk_size=131072):
    """Return the first-tool filament material declared in a G-code file, or None."""
    _, first = extract_gcode_filament_metadata(filepath, chunk_size=chunk_size)
    return first


class StateStore:
    """Atomic JSON persistence for the currently loaded filament."""

    SCHEMA_VERSION = 1

    def __init__(self, path):
        self.path = os.path.expanduser(path)

    def load(self):
        if not os.path.exists(self.path):
            return None
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, TypeError) as exc:
            logging.warning(
                "prusa_kload: ignoring unreadable state file '%s': %s",
                self.path,
                exc,
            )
            return None
        if not isinstance(data, dict):
            logging.warning(
                "prusa_kload: invalid state structure in '%s'; resetting",
                self.path,
            )
            return None
        loaded = data.get("loaded_filament")
        if isinstance(loaded, str) and loaded.strip():
            return loaded.strip().upper()
        return None

    def save(self, loaded_filament):
        parent = os.path.dirname(self.path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        payload = {
            "schema_version": self.SCHEMA_VERSION,
            "loaded_filament": loaded_filament,
            "last_updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        }
        tmp_path = "%s.tmp" % (self.path,)
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, self.path)


class PrusaKLoad:
    """Klipper extra implementing Prusa MK4S-style filament loading and checks."""

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.name = config.get_name()

        self.filament_sensor_name = config.get("filament_sensor", None)
        self.switch_pin = config.get("switch_pin", None)
        if self.filament_sensor_name and self.switch_pin:
            raise config.error(
                "prusa_kload: cannot specify both 'filament_sensor' and 'switch_pin'"
            )
        if not self.filament_sensor_name and not self.switch_pin:
            raise config.error(
                "prusa_kload: must specify either 'filament_sensor' or 'switch_pin'"
            )

        self.debounce_delay = config.getfloat("debounce_delay", 1.0, minval=0.0)

        self.load_distance = config.getfloat("load_distance", 50.0, above=0.0)
        self.load_speed = config.getfloat("load_speed", 15.0, above=0.0)
        self.purge_distance = config.getfloat("purge_distance", 30.0, minval=0.0)
        self.purge_more_distance = config.getfloat(
            "purge_more_distance", 20.0, above=0.0
        )
        self.purge_speed = config.getfloat("purge_speed", 4.0, above=0.0)
        self.unload_distance = config.getfloat("unload_distance", 80.0, above=0.0)
        self.unload_speed = config.getfloat("unload_speed", 20.0, above=0.0)
        self.ramming_distance = config.getfloat("ramming_distance", 10.0, minval=0.0)
        self.ramming_speed = config.getfloat("ramming_speed", 15.0, above=0.0)
        self.cooling_moves = config.getint("cooling_moves", 1, minval=0)

        self.cooldown_after_load = config.getboolean("cooldown_after_load", True)
        self.cooldown_after_unload = config.getboolean("cooldown_after_unload", True)
        self.prompt_timeout = config.getfloat("prompt_timeout", 300.0, minval=0.0)

        self.park_x = config.getfloat("park_x", None)
        self.park_y = config.getfloat("park_y", None)
        self.park_z = config.getfloat("park_z", None)
        self.park_speed = config.getfloat("park_speed", 100.0, above=0.0)
        self.auto_home_on_load = config.getboolean("auto_home_on_load", False)

        gcode_macro = self.printer.load_object(config, "gcode_macro")
        self.before_load_gcode = gcode_macro.load_template(
            config, "before_load_gcode", ""
        )
        self.after_load_gcode = gcode_macro.load_template(
            config, "after_load_gcode", ""
        )
        self.before_unload_gcode = gcode_macro.load_template(
            config, "before_unload_gcode", ""
        )
        self.after_unload_gcode = gcode_macro.load_template(
            config, "after_unload_gcode", ""
        )

        self.auto_load_on_insert = config.getboolean("auto_load_on_insert", True)
        self.clear_on_remove = config.getboolean("clear_on_remove", True)
        self.check_on_print_start = config.getboolean("check_on_print_start", True)
        self.override_load_unload = config.getboolean("override_load_unload", True)
        self.happy_hare_compat = config.getboolean("happy_hare_compat", True)

        state_file = config.get(
            "state_file", "~/printer_data/config/.prusa_kload_state.json"
        )
        self.state_store = StateStore(state_file)
        self.materials = MaterialRegistry()

        self.state = STATE_IDLE
        self.loaded_filament = self.state_store.load()
        if self.loaded_filament:
            self.loaded_filament = self.materials.resolve_name(self.loaded_filament)
        self.target_filament = None
        self.requested_filament = None
        self.pending_print = None

        self._select_mode = "load"  # 'load' | 'unload' | 'set'
        self._swap_after_unload = False
        self._swap_target_filament = None
        self._prompt_open = False
        self._prompt_timer = None
        self._debounce_timer = None
        self._raw_filament_present = False
        self._debounced_filament_present = None
        self._sensor_runout_helper = None
        self._busy_motion = False
        self._bypass_print_check = False
        self._orig_sdcard_print_file = None
        self._mmu = None

        if self.switch_pin is not None:
            buttons = self.printer.load_object(config, "buttons")
            buttons.register_buttons([self.switch_pin], self._button_callback)
            self._debounced_filament_present = False

        self.gcode = self.printer.lookup_object("gcode")
        self._register_commands()

        self.printer.register_event_handler("klippy:connect", self._handle_connect)
        self.printer.register_event_handler("klippy:ready", self._handle_ready)

    def _register_commands(self):
        commands = (
            ("PRUSA_KLOAD_STATUS", self.cmd_STATUS, "Report PrusaKLoad status"),
            (
                "PRUSA_KLOAD_GET_FILAMENT",
                self.cmd_GET_FILAMENT,
                "Print currently loaded filament",
            ),
            (
                "PRUSA_KLOAD_SET_FILAMENT",
                self.cmd_SET_FILAMENT,
                "Manually set currently loaded filament",
            ),
            (
                "PRUSA_KLOAD_CLEAR_FILAMENT",
                self.cmd_CLEAR_FILAMENT,
                "Clear currently loaded filament",
            ),
            (
                "PRUSA_KLOAD_LIST_MATERIALS",
                self.cmd_LIST_MATERIALS,
                "List configured filament materials",
            ),
            ("PRUSA_KLOAD_LOAD", self.cmd_LOAD, "Load filament (MK4S flow)"),
            ("PRUSA_KLOAD_UNLOAD", self.cmd_UNLOAD, "Unload filament (MK4S flow)"),
            ("PRUSA_KLOAD_SWAP", self.cmd_SWAP, "Swap to a new filament"),
            (
                "PRUSA_KLOAD_CONFIRM_PURGE",
                self.cmd_CONFIRM_PURGE,
                "Confirm purge status during filament load",
            ),
            (
                "PRUSA_KLOAD_MISMATCH_ACTION",
                self.cmd_MISMATCH_ACTION,
                "Respond to pre-print material mismatch prompt",
            ),
            (
                "PRUSA_KLOAD_CHECK",
                self.cmd_CHECK,
                "Check requested material against loaded filament",
            ),
            (
                "PRUSA_KLOAD_CANCEL",
                self.cmd_CANCEL,
                "Cancel an active PrusaKLoad operation",
            ),
        )
        for name, handler, desc in commands:
            self.gcode.register_command(name, handler, desc=desc)

    def register_material_config(self, config):
        """Register or override a [prusa_kload_material <NAME>] section."""
        sec_name = config.get_name()
        parts = sec_name.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            raise config.error(
                "Section '%s' must specify a material name" % (sec_name,)
            )
        raw_name = parts[1].strip().upper()
        existing = self.materials.materials.get(raw_name)

        if existing is not None:
            load_temp = config.getfloat("load_temp", existing.load_temp, above=0.0)
            unload_temp = config.getfloat(
                "unload_temp", load_temp, above=0.0
            ) if config.get("unload_temp", None) is not None else (
                load_temp
                if config.get("load_temp", None) is not None
                else existing.unload_temp
            )
            purge_distance = config.getfloat(
                "purge_distance", existing.purge_distance, minval=0.0
            )
            load_speed = config.getfloat(
                "load_speed", existing.load_speed, above=0.0
            )
            raw_aliases = config.get("aliases", None)
            aliases = (
                [a.strip() for a in raw_aliases.split(",") if a.strip()]
                if raw_aliases is not None
                else existing.aliases
            )
        else:
            load_temp = config.getfloat("load_temp", above=0.0)
            unload_temp = config.getfloat("unload_temp", load_temp, above=0.0)
            purge_distance = config.getfloat("purge_distance", None, minval=0.0)
            load_speed = config.getfloat("load_speed", None, above=0.0)
            raw_aliases = config.get("aliases", "")
            aliases = [a.strip() for a in raw_aliases.split(",") if a.strip()]

        return self.materials.register_material(
            name=raw_name,
            load_temp=load_temp,
            unload_temp=unload_temp,
            purge_distance=purge_distance,
            load_speed=load_speed,
            aliases=aliases,
        )

    # ------------------------------------------------------------------
    # Klipper lifecycle & sensor hooks
    # ------------------------------------------------------------------

    def _lookup_filament_sensor(self):
        candidates = [self.filament_sensor_name]
        if " " not in self.filament_sensor_name:
            candidates.extend(
                [
                    "filament_switch_sensor %s" % self.filament_sensor_name,
                    "filament_motion_sensor %s" % self.filament_sensor_name,
                ]
            )
        for name in candidates:
            sensor = self.printer.lookup_object(name, None)
            if sensor is not None:
                return sensor
        raise self.printer.config_error(
            "prusa_kload: filament_sensor '%s' not found"
            % (self.filament_sensor_name,)
        )

    def _handle_connect(self):
        if self.happy_hare_compat:
            self._mmu = self.printer.lookup_object("mmu", None)
        if self.filament_sensor_name is not None:
            sensor = self._lookup_filament_sensor()
            runout_helper = getattr(sensor, "runout_helper", None)
            if runout_helper is None:
                raise self.printer.config_error(
                    "prusa_kload: sensor '%s' has no runout_helper"
                    % (self.filament_sensor_name,)
                )
            self._sensor_runout_helper = runout_helper
            initial = bool(getattr(runout_helper, "filament_present", False))
            self._raw_filament_present = initial
            self._debounced_filament_present = initial

            orig_note = runout_helper.note_filament_present

            def wrapped_note_filament_present(*args, **kwargs):
                res = orig_note(*args, **kwargs)
                if len(args) >= 2:
                    ev_time, present = args[0], bool(args[1])
                elif len(args) == 1:
                    ev_time, present = self.reactor.monotonic(), bool(args[0])
                else:
                    ev_time = kwargs.get("eventtime", self.reactor.monotonic())
                    present = bool(
                        kwargs.get(
                            "is_filament_present",
                            getattr(runout_helper, "filament_present", False),
                        )
                    )
                self._on_raw_sensor_change(ev_time, present)
                return res

            runout_helper.note_filament_present = wrapped_note_filament_present

    def _is_happy_hare_active(self, eventtime=None):
        if self._mmu is None:
            return False
        et = self.reactor.monotonic() if eventtime is None else eventtime
        status = self._mmu.get_status(et) if hasattr(self._mmu, "get_status") else {}
        if not status.get("enabled", True):
            return False
        if status.get("gate", -1) == -2:
            return False
        return True

    def _warn_happy_hare_active(self, gcmd=None):
        msg = (
            "PrusaKLoad: Happy Hare (MMU) is active; switch to bypass mode "
            "or disable MMU to use PrusaKLoad."
        )
        if gcmd is not None:
            gcmd.respond_info(msg)
        else:
            self.gcode.respond_info(msg)

    def _register_override(self, cmd_name, handler, desc):
        orig = self.gcode.register_command(cmd_name, None)

        def wrapped(gcmd):
            if self._is_happy_hare_active():
                if orig is not None:
                    return orig(gcmd)
                self._warn_happy_hare_active(gcmd)
                return None
            return handler(gcmd)

        self.gcode.register_command(cmd_name, wrapped, desc=desc)

    def _handle_ready(self):
        if self._mmu is not None:
            msg = (
                "PrusaKLoad: Happy Hare (MMU) detected; standing down while MMU "
                "is active (will activate automatically in bypass or disabled mode)."
            )
            logging.warning(msg)
            self.gcode.respond_info(msg)
        if self._orig_sdcard_print_file is None:
            orig = self.gcode.register_command("SDCARD_PRINT_FILE", None)
            if orig is not None:
                self._orig_sdcard_print_file = orig
                self.gcode.register_command(
                    "SDCARD_PRINT_FILE",
                    self.cmd_SDCARD_PRINT_FILE,
                    desc="Start printing a file from SD card (with PrusaKLoad check)",
                )
        if self.override_load_unload:
            self._register_override(
                "LOAD_FILAMENT",
                self.cmd_LOAD,
                "Load filament (PrusaKLoad MK4S flow)",
            )
            self._register_override(
                "UNLOAD_FILAMENT",
                self.cmd_UNLOAD,
                "Unload filament (PrusaKLoad MK4S flow)",
            )

    def _button_callback(self, eventtime, state):
        self._on_raw_sensor_change(eventtime, bool(state))

    def _cancel_debounce_timer(self):
        if self._debounce_timer is not None:
            self.reactor.unregister_timer(self._debounce_timer)
            self._debounce_timer = None

    def _on_raw_sensor_change(self, eventtime, is_present):
        is_present = bool(is_present)
        if (
            is_present == self._raw_filament_present
            and self._debounce_timer is None
            and is_present == self._debounced_filament_present
        ):
            return
        self._raw_filament_present = is_present
        self._cancel_debounce_timer()
        if self.debounce_delay <= 0.0:
            self.reactor.register_callback(
                lambda et, val=is_present: self._on_debounced_sensor_state(et, val)
            )
            return
        self._debounce_timer = self.reactor.register_timer(
            self._debounce_timer_callback, eventtime + self.debounce_delay
        )

    def _debounce_timer_callback(self, eventtime):
        self._debounce_timer = None
        self._on_debounced_sensor_state(eventtime, self._raw_filament_present)
        return self.reactor.NEVER

    def _get_print_state(self, eventtime=None):
        print_stats = self.printer.lookup_object("print_stats", None)
        if print_stats is None:
            return "standby"
        et = self.reactor.monotonic() if eventtime is None else eventtime
        return print_stats.get_status(et).get("state", "standby")

    def _is_filament_present(self):
        if self._sensor_runout_helper is not None:
            return bool(getattr(self._sensor_runout_helper, "filament_present", False))
        return self._debounced_filament_present

    def _on_debounced_sensor_state(self, eventtime, present):
        present = bool(present)
        if present == self._debounced_filament_present:
            return
        self._debounced_filament_present = present

        if self._is_happy_hare_active(eventtime):
            return

        print_state = self._get_print_state(eventtime)
        if print_state == "printing":
            return
        if print_state == "paused":
            if self.state == STATE_SWAP_WAIT_INSERT and present:
                self._handle_swap_insert(from_command=False)
            elif self.state == STATE_WAIT_REMOVE and not present:
                self._finalize_unload(from_command=False)
            return

        if present:
            if self.state == STATE_SWAP_WAIT_INSERT:
                self._handle_swap_insert(from_command=False)
            elif (
                self.state == STATE_IDLE
                and self.auto_load_on_insert
                and self.loaded_filament is None
            ):
                self._open_material_picker(mode="load")
        else:
            if self.state == STATE_WAIT_REMOVE:
                self._finalize_unload(from_command=False)
            elif (
                self.state == STATE_IDLE
                and self.clear_on_remove
                and self.loaded_filament is not None
            ):
                self._set_loaded_filament(None)
                self.gcode.respond_info(
                    "PrusaKLoad: filament removed; cleared loaded filament"
                )
                self._set_lcd_status("Filament removed", from_command=False)

    # ------------------------------------------------------------------
    # Status, Persistence, Prompt & LCD Helpers
    # ------------------------------------------------------------------

    def get_status(self, eventtime):
        pending_fn = (
            self.pending_print.get("filename") if self.pending_print else None
        )
        return {
            "state": self.state,
            "loaded_filament": self.loaded_filament,
            "target_filament": self.target_filament,
            "requested_filament": self.requested_filament,
            "pending_filename": pending_fn,
            "filament_present": self._is_filament_present(),
            "materials": self.materials.list_names(),
            "happy_hare_detected": self._mmu is not None,
            "happy_hare_active": self._is_happy_hare_active(eventtime),
        }

    def _set_loaded_filament(self, material):
        self.loaded_filament = material
        self.state_store.save(material)

    def _run_gcode(self, script, from_command=False):
        if not script or not script.strip():
            return
        if from_command and hasattr(self.gcode, "run_script_from_command"):
            self.gcode.run_script_from_command(script)
        else:
            self.gcode.run_script(script)

    def _run_template(self, template, from_command=False):
        if template is None:
            return
        rendered = template.render()
        if rendered and rendered.strip():
            self._run_gcode(rendered, from_command=from_command)

    def _set_lcd_status(self, text, from_command=False):
        short = str(text)[:20]
        try:
            self._run_gcode("M117 %s" % (short,), from_command=from_command)
        except Exception as exc:
            logging.debug("prusa_kload: M117 failed: %s", exc)

    def _prompt_line(self, msg):
        self.gcode.respond_raw("// %s" % (msg,))

    def _close_prompt(self):
        self._cancel_prompt_timeout()
        if self._prompt_open:
            self._prompt_line("action:prompt_end")
            self._prompt_open = False

    def _cancel_prompt_timeout(self):
        if self._prompt_timer is not None:
            self.reactor.unregister_timer(self._prompt_timer)
            self._prompt_timer = None

    def _start_prompt_timeout(self):
        self._cancel_prompt_timeout()
        if self.prompt_timeout <= 0.0:
            return
        deadline = self.reactor.monotonic() + self.prompt_timeout
        self._prompt_timer = self.reactor.register_timer(
            self._on_prompt_timeout, deadline
        )

    def _on_prompt_timeout(self, eventtime):
        self._prompt_timer = None
        if self.state == STATE_IDLE:
            return self.reactor.NEVER
        timed_out_state = self.state
        self._close_prompt()
        self.gcode.respond_info(
            "PrusaKLoad: prompt timed out in state %s; cancelling and cooling down"
            % (timed_out_state,)
        )
        self._abort_to_idle(cooldown=True, from_command=False)
        self._set_lcd_status("KLoad timeout", from_command=False)
        return self.reactor.NEVER

    def _open_material_picker(self, mode="load", from_command=False):
        self._select_mode = mode
        self.state = STATE_SELECT_MATERIAL
        self._close_prompt()

        if mode == "unload":
            title = "PrusaKLoad: Select Unload Material"
            text = "Loaded filament unknown. Choose material temperature for unload:"
            cmd_prefix = "PRUSA_KLOAD_UNLOAD MATERIAL="
            lcd_msg = "Unload: Pick Mat"
        elif mode == "set":
            title = "PrusaKLoad: Set Loaded Filament"
            text = "Select the filament currently loaded in the printer:"
            cmd_prefix = "PRUSA_KLOAD_SET_FILAMENT MATERIAL="
            lcd_msg = "Set: Pick Mat"
        else:
            title = "PrusaKLoad: Select Filament"
            text = "Filament detected. Choose the material you inserted:"
            cmd_prefix = "PRUSA_KLOAD_LOAD MATERIAL="
            lcd_msg = "Load: Pick Mat"

        self._prompt_line("action:prompt_begin %s" % (title,))
        self._prompt_line("action:prompt_text %s" % (text,))
        names = self.materials.list_names()
        row_size = 4
        for idx in range(0, len(names), row_size):
            self._prompt_line("action:prompt_button_group_start")
            for name in names[idx : idx + row_size]:
                self._prompt_line(
                    "action:prompt_button %s|%s%s|primary" % (name, cmd_prefix, name)
                )
            self._prompt_line("action:prompt_button_group_end")
        if mode == "set":
            self._prompt_line(
                "action:prompt_footer_button Clear (None)|PRUSA_KLOAD_CLEAR_FILAMENT|secondary"
            )
        self._prompt_line(
            "action:prompt_footer_button Cancel|PRUSA_KLOAD_CANCEL|warning"
        )
        self._prompt_line("action:prompt_show")
        self._prompt_open = True
        self._start_prompt_timeout()
        self._set_lcd_status(lcd_msg, from_command=from_command)

    def _show_purge_prompt(self, material_name, from_command=False):
        self._close_prompt()
        self._prompt_line(
            "action:prompt_begin PrusaKLoad: Purge Check (%s)" % (material_name,)
        )
        self._prompt_line(
            "action:prompt_text Extruded %s. Is the purged color clear and clean?"
            % (material_name,)
        )
        self._prompt_line("action:prompt_button_group_start")
        self._prompt_line(
            "action:prompt_button Yes|PRUSA_KLOAD_CONFIRM_PURGE ACTION=YES|primary"
        )
        self._prompt_line(
            "action:prompt_button Purge More|PRUSA_KLOAD_CONFIRM_PURGE ACTION=PURGE_MORE|secondary"
        )
        self._prompt_line(
            "action:prompt_button Retry|PRUSA_KLOAD_CONFIRM_PURGE ACTION=RETRY|secondary"
        )
        self._prompt_line("action:prompt_button_group_end")
        self._prompt_line(
            "action:prompt_footer_button Cancel|PRUSA_KLOAD_CANCEL|warning"
        )
        self._prompt_line("action:prompt_show")
        self._prompt_open = True
        self._start_prompt_timeout()
        self._set_lcd_status("Color clear?", from_command=from_command)

    def _show_mismatch_prompt(self, requested, loaded, from_command=False):
        self._close_prompt()
        loaded_str = loaded if loaded else "None / Unknown"
        self._prompt_line("action:prompt_begin PrusaKLoad: Material Mismatch!")
        self._prompt_line(
            "action:prompt_text G-code requests: %s | Currently loaded: %s"
            % (requested, loaded_str)
        )
        self._prompt_line("action:prompt_button_group_start")
        self._prompt_line(
            "action:prompt_button Swap|PRUSA_KLOAD_MISMATCH_ACTION ACTION=SWAP|primary"
        )
        self._prompt_line(
            "action:prompt_button Force|PRUSA_KLOAD_MISMATCH_ACTION ACTION=FORCE|warning"
        )
        self._prompt_line(
            "action:prompt_button Cancel|PRUSA_KLOAD_MISMATCH_ACTION ACTION=CANCEL|error"
        )
        self._prompt_line("action:prompt_button_group_end")
        self._prompt_line("action:prompt_show")
        self._prompt_open = True
        self._start_prompt_timeout()
        self._set_lcd_status(
            "Mat: %s!=%s" % (loaded or "None", requested),
            from_command=from_command,
        )

    def _show_wait_remove_prompt(self, from_command=False):
        self._close_prompt()
        self._prompt_line("action:prompt_begin PrusaKLoad: Remove Filament")
        self._prompt_line(
            "action:prompt_text Filament retracted. Pull filament out of the sensor."
        )
        self._prompt_line(
            "action:prompt_footer_button Done|PRUSA_KLOAD_UNLOAD CONFIRM=1|primary"
        )
        self._prompt_line(
            "action:prompt_footer_button Cancel|PRUSA_KLOAD_CANCEL|warning"
        )
        self._prompt_line("action:prompt_show")
        self._prompt_open = True
        self._start_prompt_timeout()
        self._set_lcd_status("Pull filament out", from_command=from_command)

    def _show_swap_wait_insert_prompt(self, target_material, from_command=False):
        self._close_prompt()
        self._prompt_line(
            "action:prompt_begin PrusaKLoad: Insert %s" % (target_material,)
        )
        self._prompt_line(
            "action:prompt_text Insert %s filament to continue print."
            % (target_material,)
        )
        if self.materials.get(target_material) is not None:
            continue_cmd = "PRUSA_KLOAD_LOAD MATERIAL=%s" % (target_material,)
        else:
            continue_cmd = "PRUSA_KLOAD_LOAD"
        self._prompt_line(
            "action:prompt_footer_button Loaded / Continue|%s|primary"
            % (continue_cmd,)
        )
        self._prompt_line(
            "action:prompt_footer_button Cancel|PRUSA_KLOAD_CANCEL|warning"
        )
        self._prompt_line("action:prompt_show")
        self._prompt_open = True
        self._start_prompt_timeout()
        self._set_lcd_status(
            "Insert %s" % (target_material,), from_command=from_command
        )

    # ------------------------------------------------------------------
    # Motion, Thermal & Workflow Execution
    # ------------------------------------------------------------------

    def _validate_extrude_temp(self, target_temp):
        extruder = self.printer.lookup_object("extruder", None)
        if extruder is None:
            return
        heater = extruder.get_heater() if hasattr(extruder, "get_heater") else None
        if heater is None:
            return
        min_temp = getattr(heater, "min_extrude_temp", None)
        if min_temp is not None and target_temp < min_temp:
            raise self.gcode.error(
                "PrusaKLoad: target temperature %.1fC is below extruder min_extrude_temp %.1fC"
                % (target_temp, min_temp)
            )

    def _maybe_park(self, from_command=False):
        if self.park_x is None and self.park_y is None and self.park_z is None:
            return
        toolhead = self.printer.lookup_object("toolhead", None)
        if toolhead is None:
            return
        et = self.reactor.monotonic()
        status = toolhead.get_status(et)
        homed = str(status.get("homed_axes", "")).lower()
        need_xy = self.park_x is not None or self.park_y is not None
        need_z = self.park_z is not None
        missing_axes = (need_xy and ("x" not in homed or "y" not in homed)) or (
            need_z and "z" not in homed
        )
        if missing_axes:
            if not self.auto_home_on_load:
                return
            self._run_gcode("G28", from_command=from_command)

        gcode_move = self.printer.lookup_object("gcode_move", None)
        was_abs_pos = (
            gcode_move.get_status(et).get("absolute_coordinates", True)
            if gcode_move
            else True
        )
        feed = self.park_speed * 60.0
        try:
            self._run_gcode("G90", from_command=from_command)
            if self.park_z is not None:
                pos = status.get("position", [0.0, 0.0, 0.0, 0.0])
                cur_z = pos[2] if len(pos) > 2 else 0.0
                if cur_z < self.park_z or missing_axes:
                    self._run_gcode(
                        "G1 Z%.3f F%.1f" % (self.park_z, feed),
                        from_command=from_command,
                    )
            axes = []
            if self.park_x is not None:
                axes.append("X%.3f" % (self.park_x,))
            if self.park_y is not None:
                axes.append("Y%.3f" % (self.park_y,))
            if axes:
                self._run_gcode(
                    "G1 %s F%.1f" % (" ".join(axes), feed),
                    from_command=from_command,
                )
        finally:
            if not was_abs_pos:
                self._run_gcode("G91", from_command=from_command)

    def _with_relative_extrusion(self, fn, from_command=False):
        gcode_move = self.printer.lookup_object("gcode_move", None)
        et = self.reactor.monotonic()
        was_abs = (
            gcode_move.get_status(et).get("absolute_extrude", True)
            if gcode_move
            else True
        )
        self._run_gcode("M83", from_command=from_command)
        try:
            fn()
        finally:
            if was_abs:
                self._run_gcode("M82", from_command=from_command)

    def _get_max_extrude_chunk(self):
        extruder = self.printer.lookup_object("extruder", None)
        max_dist = getattr(extruder, "max_e_dist", None) if extruder else None
        if max_dist is not None and max_dist > 0.0:
            return float(max_dist)
        return None

    def _extrude_chunked(self, distance, speed_mm_s, from_command=False):
        if distance == 0.0:
            return
        feed = speed_mm_s * 60.0
        max_chunk = self._get_max_extrude_chunk()
        remaining = abs(float(distance))
        sign = 1.0 if distance > 0.0 else -1.0
        if max_chunk is None or remaining <= max_chunk:
            self._run_gcode(
                "G1 E%.3f F%.1f" % (sign * remaining, feed),
                from_command=from_command,
            )
            return
        while remaining > 1e-6:
            step = min(remaining, max_chunk)
            self._run_gcode(
                "G1 E%.3f F%.1f" % (sign * step, feed),
                from_command=from_command,
            )
            remaining -= step

    def _cooldown(self, from_command=False):
        self._run_gcode("M104 S0", from_command=from_command)

    def _abort_to_idle(self, cooldown=False, from_command=False):
        self._close_prompt()
        self.pending_print = None
        self.target_filament = None
        self.requested_filament = None
        self._swap_after_unload = False
        self._swap_target_filament = None
        self.state = STATE_IDLE
        if cooldown:
            self._cooldown(from_command=from_command)

    def _execute_load_sequence(self, material_name, from_command=False):
        if self._busy_motion:
            raise self.gcode.error("PrusaKLoad: operation already in progress")
        profile = self.materials.get(material_name)
        if profile is None:
            raise self.gcode.error(
                "PrusaKLoad: unknown material '%s'" % (material_name,)
            )
        self._validate_extrude_temp(profile.load_temp)
        self._busy_motion = True
        try:
            self._close_prompt()
            self.target_filament = profile.name
            self._maybe_park(from_command=from_command)
            self._run_template(self.before_load_gcode, from_command=from_command)

            self.state = STATE_HEATING_LOAD
            self._set_lcd_status(
                "Heat %s %dC" % (profile.name, int(round(profile.load_temp))),
                from_command=from_command,
            )
            self._run_gcode(
                "M109 S%.1f" % (profile.load_temp,), from_command=from_command
            )

            self.state = STATE_LOADING
            self._set_lcd_status("Loading %s" % (profile.name,), from_command=from_command)
            load_speed = profile.load_speed or self.load_speed
            purge_dist = (
                profile.purge_distance
                if profile.purge_distance is not None
                else self.purge_distance
            )

            def do_extrude():
                self._extrude_chunked(
                    self.load_distance, load_speed, from_command=from_command
                )
                if purge_dist > 0.0:
                    self._extrude_chunked(
                        purge_dist, self.purge_speed, from_command=from_command
                    )

            self._with_relative_extrusion(do_extrude, from_command=from_command)
            self.state = STATE_CONFIRM_PURGE
            self._show_purge_prompt(profile.name, from_command=from_command)
        except Exception:
            self.state = STATE_IDLE
            raise
        finally:
            self._busy_motion = False

    def _execute_unload_sequence(
        self, material_name=None, swap_after_unload=False, from_command=False
    ):
        if self._busy_motion:
            raise self.gcode.error("PrusaKLoad: operation already in progress")

        lookup_name = material_name or self.loaded_filament
        profile = self.materials.get(lookup_name) if lookup_name else None
        if profile is None:
            self._swap_after_unload = swap_after_unload
            self._open_material_picker(mode="unload", from_command=from_command)
            return

        self._validate_extrude_temp(profile.unload_temp)
        self._busy_motion = True
        try:
            self._close_prompt()
            self._swap_after_unload = swap_after_unload
            self.target_filament = profile.name
            self._maybe_park(from_command=from_command)
            self._run_template(self.before_unload_gcode, from_command=from_command)

            self.state = STATE_HEATING_UNLOAD
            self._set_lcd_status(
                "Unload %dC" % (int(round(profile.unload_temp)),),
                from_command=from_command,
            )
            self._run_gcode(
                "M109 S%.1f" % (profile.unload_temp,), from_command=from_command
            )

            self.state = STATE_UNLOADING
            self._set_lcd_status("Unloading...", from_command=from_command)

            def do_retract():
                if self.ramming_distance > 0.0:
                    self._extrude_chunked(
                        self.ramming_distance,
                        self.ramming_speed,
                        from_command=from_command,
                    )
                    self._extrude_chunked(
                        -self.ramming_distance,
                        self.ramming_speed,
                        from_command=from_command,
                    )
                if self.cooling_moves > 0:
                    stroke = min(5.0, self.unload_distance * 0.25)
                    for _ in range(self.cooling_moves):
                        self._extrude_chunked(
                            stroke, self.ramming_speed, from_command=from_command
                        )
                        self._extrude_chunked(
                            -stroke, self.ramming_speed, from_command=from_command
                        )
                self._extrude_chunked(
                    -self.unload_distance,
                    self.unload_speed,
                    from_command=from_command,
                )

            self._with_relative_extrusion(do_retract, from_command=from_command)

            if self._is_filament_present() is False:
                self._finalize_unload(from_command=from_command)
            else:
                self.state = STATE_WAIT_REMOVE
                self._show_wait_remove_prompt(from_command=from_command)
        except Exception:
            self.state = STATE_IDLE
            raise
        finally:
            self._busy_motion = False

    def _finalize_unload(self, from_command=False):
        self._close_prompt()
        self._set_loaded_filament(None)
        self._run_template(self.after_unload_gcode, from_command=from_command)

        if self._swap_after_unload:
            self._swap_after_unload = False
            swap_target = self._swap_target_filament or self.requested_filament
            self.target_filament = swap_target
            self.state = STATE_SWAP_WAIT_INSERT
            if swap_target:
                self._show_swap_wait_insert_prompt(
                    swap_target, from_command=from_command
                )
            else:
                self._open_material_picker(mode="load", from_command=from_command)
            return

        self.target_filament = None
        self.state = STATE_IDLE
        if self.cooldown_after_unload:
            self._cooldown(from_command=from_command)
        self._set_lcd_status("Unload complete", from_command=from_command)

    def _handle_swap_insert(self, from_command=False):
        target = self._swap_target_filament or self.target_filament or self.requested_filament
        if target and self.materials.get(target) is not None:
            self._execute_load_sequence(target, from_command=from_command)
        else:
            self._open_material_picker(mode="load", from_command=from_command)

    def _launch_pending_print(self, pending, from_command=False):
        if not pending:
            return
        mode = pending.get("mode")
        if mode == "sdcard":
            gcmd = pending.get("gcmd")
            if gcmd is not None and self._orig_sdcard_print_file is not None:
                self._bypass_print_check = True
                try:
                    self._orig_sdcard_print_file(gcmd)
                finally:
                    self._bypass_print_check = False
        elif mode == "pause":
            self._run_gcode("RESUME", from_command=from_command)

    # ------------------------------------------------------------------
    # SDCARD_PRINT_FILE & G-code Command Handlers
    # ------------------------------------------------------------------

    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        if (
            self._bypass_print_check
            or not self.check_on_print_start
            or self._is_happy_hare_active()
        ):
            return self._orig_sdcard_print_file(gcmd)

        filename = gcmd.get("FILENAME", "")
        vsd = self.printer.lookup_object("virtual_sdcard", None)
        if filename and vsd is not None and hasattr(vsd, "sdcard_dirname"):
            full_path = os.path.join(vsd.sdcard_dirname, filename.lstrip("/"))
            if os.path.isfile(full_path):
                requested_raw = parse_gcode_filament(full_path)
                is_match, canonical_req, _ = self.materials.matches(
                    requested_raw, self.loaded_filament
                )
                if not is_match:
                    self.pending_print = {
                        "mode": "sdcard",
                        "gcmd": gcmd,
                        "filename": filename,
                        "requested_material": canonical_req,
                    }
                    self.requested_filament = canonical_req
                    self.state = STATE_MISMATCH_PROMPT
                    self._show_mismatch_prompt(
                        canonical_req, self.loaded_filament, from_command=True
                    )
                    return None
        return self._orig_sdcard_print_file(gcmd)

    def cmd_STATUS(self, gcmd):
        status = self.get_status(self.reactor.monotonic())
        hh_state = (
            "active"
            if status["happy_hare_active"]
            else ("bypass" if status["happy_hare_detected"] else "none")
        )
        gcmd.respond_info(
            "PrusaKLoad state=%s loaded=%s target=%s requested=%s sensor=%s happy_hare=%s"
            % (
                status["state"],
                status["loaded_filament"],
                status["target_filament"],
                status["requested_filament"],
                status["filament_present"],
                hh_state,
            )
        )

    def cmd_GET_FILAMENT(self, gcmd):
        gcmd.respond_info(
            "Loaded filament: %s" % (self.loaded_filament or "None",)
        )

    def cmd_SET_FILAMENT(self, gcmd):
        if self._is_happy_hare_active():
            self._warn_happy_hare_active(gcmd)
            return
        raw = gcmd.get("MATERIAL", None)
        if raw is None or not raw.strip():
            self._open_material_picker(mode="set", from_command=True)
            return
        canonical = self.materials.resolve_name(raw)
        if not canonical:
            raise self.gcode.error("Invalid material name '%s'" % (raw,))
        self._close_prompt()
        self._set_loaded_filament(canonical)
        if self.state == STATE_SELECT_MATERIAL and self._select_mode == "set":
            self.state = STATE_IDLE
        gcmd.respond_info("PrusaKLoad: loaded filament set to %s" % (canonical,))
        self._set_lcd_status("Loaded: %s" % (canonical,), from_command=True)

    def cmd_CLEAR_FILAMENT(self, gcmd):
        if self._is_happy_hare_active():
            self._warn_happy_hare_active(gcmd)
            return
        self._close_prompt()
        self._set_loaded_filament(None)
        if self.state == STATE_SELECT_MATERIAL and self._select_mode == "set":
            self.state = STATE_IDLE
        gcmd.respond_info("PrusaKLoad: cleared loaded filament")
        self._set_lcd_status("Loaded: None", from_command=True)

    def cmd_LIST_MATERIALS(self, gcmd):
        lines = []
        for name in self.materials.list_names():
            prof = self.materials.materials[name]
            alias_str = ",".join(prof.aliases) if prof.aliases else "-"
            lines.append(
                "%s: load=%.0fC unload=%.0fC aliases=[%s]"
                % (prof.name, prof.load_temp, prof.unload_temp, alias_str)
            )
        gcmd.respond_info("\n".join(lines))

    def cmd_LOAD(self, gcmd):
        if self._is_happy_hare_active():
            self._warn_happy_hare_active(gcmd)
            return
        raw_mat = gcmd.get("MATERIAL", None)
        if self.state == STATE_SELECT_MATERIAL and self._select_mode == "unload":
            if raw_mat and raw_mat.strip():
                self._execute_unload_sequence(
                    material_name=raw_mat,
                    swap_after_unload=self._swap_after_unload,
                    from_command=True,
                )
                return

        if raw_mat is None or not raw_mat.strip():
            self._open_material_picker(mode="load", from_command=True)
            return

        self._execute_load_sequence(raw_mat, from_command=True)

    def cmd_UNLOAD(self, gcmd):
        if self._is_happy_hare_active():
            self._warn_happy_hare_active(gcmd)
            return
        confirm = gcmd.get_int("CONFIRM", 0)
        if confirm:
            if self.state == STATE_WAIT_REMOVE:
                self._finalize_unload(from_command=True)
            return
        raw_mat = gcmd.get("MATERIAL", None)
        self._execute_unload_sequence(
            material_name=raw_mat,
            swap_after_unload=False,
            from_command=True,
        )

    def cmd_SWAP(self, gcmd):
        if self._is_happy_hare_active():
            self._warn_happy_hare_active(gcmd)
            return
        raw_mat = gcmd.get("MATERIAL", None)
        target = self.materials.resolve_name(raw_mat) if raw_mat else None
        if raw_mat and self.materials.get(target) is None:
            raise self.gcode.error("PrusaKLoad: unknown material '%s'" % (raw_mat,))
        self._swap_target_filament = target
        self.target_filament = target

        if self.loaded_filament is not None or self._is_filament_present():
            self._execute_unload_sequence(
                material_name=self.loaded_filament,
                swap_after_unload=True,
                from_command=True,
            )
        else:
            self.state = STATE_SWAP_WAIT_INSERT
            if target:
                self._show_swap_wait_insert_prompt(target, from_command=True)
            else:
                self._open_material_picker(mode="load", from_command=True)

    def cmd_CONFIRM_PURGE(self, gcmd):
        if self._is_happy_hare_active():
            self._warn_happy_hare_active(gcmd)
            return
        if self.state != STATE_CONFIRM_PURGE:
            raise self.gcode.error(
                "PrusaKLoad: not awaiting purge confirmation (state=%s)"
                % (self.state,)
            )
        action = gcmd.get("ACTION").strip().upper()
        profile = self.materials.get(self.target_filament)
        if profile is None:
            raise self.gcode.error("PrusaKLoad: active target material missing")

        if action == "PURGE_MORE":
            self._close_prompt()
            self._set_lcd_status("Purging more...", from_command=True)
            self._with_relative_extrusion(
                lambda: self._extrude_chunked(
                    self.purge_more_distance,
                    self.purge_speed,
                    from_command=True,
                ),
                from_command=True,
            )
            self._show_purge_prompt(profile.name, from_command=True)
        elif action == "RETRY":
            self._close_prompt()
            self._execute_load_sequence(profile.name, from_command=True)
        elif action == "YES":
            self._close_prompt()
            self._set_loaded_filament(profile.name)
            self._run_template(self.after_load_gcode, from_command=True)
            pending = self.pending_print
            self.pending_print = None
            self.target_filament = None
            self.requested_filament = None
            self._swap_target_filament = None
            self.state = STATE_IDLE
            if pending is not None:
                self._set_lcd_status("Starting print", from_command=True)
                self._launch_pending_print(pending, from_command=True)
            else:
                if self.cooldown_after_load:
                    self._cooldown(from_command=True)
                self._set_lcd_status("Loaded %s" % (profile.name,), from_command=True)
        else:
            raise self.gcode.error(
                "PrusaKLoad: invalid CONFIRM_PURGE ACTION '%s'" % (action,)
            )

    def cmd_MISMATCH_ACTION(self, gcmd):
        if self._is_happy_hare_active():
            self._warn_happy_hare_active(gcmd)
            return
        if self.state != STATE_MISMATCH_PROMPT:
            raise self.gcode.error(
                "PrusaKLoad: no active mismatch prompt (state=%s)" % (self.state,)
            )
        action = gcmd.get("ACTION").strip().upper()
        pending = self.pending_print
        self._close_prompt()

        if action == "CANCEL":
            self.pending_print = None
            self.requested_filament = None
            self.target_filament = None
            self._swap_target_filament = None
            self.state = STATE_IDLE
            self._set_lcd_status("Print cancelled", from_command=True)
            if pending and pending.get("mode") == "pause":
                self._run_gcode("CANCEL_PRINT", from_command=True)
        elif action == "FORCE":
            self.pending_print = None
            self.requested_filament = None
            self.target_filament = None
            self._swap_target_filament = None
            self.state = STATE_IDLE
            self._set_lcd_status("Force starting", from_command=True)
            self._launch_pending_print(pending, from_command=True)
        elif action == "SWAP":
            req_mat = pending.get("requested_material") if pending else self.requested_filament
            self._swap_target_filament = req_mat
            self.target_filament = req_mat
            if self.loaded_filament is not None or self._is_filament_present():
                self._execute_unload_sequence(
                    material_name=self.loaded_filament,
                    swap_after_unload=True,
                    from_command=True,
                )
            else:
                self.state = STATE_SWAP_WAIT_INSERT
                if req_mat:
                    self._show_swap_wait_insert_prompt(req_mat, from_command=True)
                else:
                    self._open_material_picker(mode="load", from_command=True)
        else:
            raise self.gcode.error(
                "PrusaKLoad: invalid MISMATCH_ACTION '%s'" % (action,)
            )

    def cmd_CHECK(self, gcmd):
        if self._is_happy_hare_active():
            self._warn_happy_hare_active(gcmd)
            return
        raw_mat = gcmd.get("MATERIAL", "").strip()
        is_match, canonical_req, reason = self.materials.matches(
            raw_mat, self.loaded_filament
        )
        if is_match:
            gcmd.respond_info(
                "PrusaKLoad: material check passed (%s)" % (reason,)
            )
            return

        print_state = self._get_print_state()
        mode = "pause" if print_state == "printing" else "check"
        self.pending_print = {
            "mode": mode,
            "gcmd": None,
            "filename": None,
            "requested_material": canonical_req,
        }
        self.requested_filament = canonical_req
        self.state = STATE_MISMATCH_PROMPT
        if mode == "pause":
            self._run_gcode("PAUSE", from_command=True)
        self._show_mismatch_prompt(
            canonical_req, self.loaded_filament, from_command=True
        )

    def cmd_CANCEL(self, gcmd):
        was_hot = self.state in (
            STATE_HEATING_LOAD,
            STATE_LOADING,
            STATE_CONFIRM_PURGE,
            STATE_HEATING_UNLOAD,
            STATE_UNLOADING,
            STATE_WAIT_REMOVE,
            STATE_SWAP_WAIT_INSERT,
        )
        pending = self.pending_print
        self._abort_to_idle(cooldown=was_hot, from_command=True)
        if pending and pending.get("mode") == "pause":
            self._run_gcode("CANCEL_PRINT", from_command=True)
        gcmd.respond_info("PrusaKLoad: operation cancelled")
        self._set_lcd_status("KLoad cancelled", from_command=True)


def load_config(config):
    return PrusaKLoad(config)
