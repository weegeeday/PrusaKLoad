import os
import shlex
import sys
import pytest

EXTRAS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(__file__), "..", "klippy", "extras")
)
if EXTRAS_DIR not in sys.path:
    sys.path.insert(0, EXTRAS_DIR)

import prusa_kload  # noqa: E402
import prusa_kload_material  # noqa: E402


class ConfigError(Exception):
    pass


class GCodeError(Exception):
    pass


class FakeTimer:
    def __init__(self, callback, waketime):
        self.callback = callback
        self.waketime = waketime
        self.cancelled = False


class FakeReactor:
    NOW = 0.0
    NEVER = 999999999999.0

    def __init__(self):
        self.now = 100.0
        self.timers = []
        self.callbacks = []

    def monotonic(self):
        return self.now

    def register_timer(self, callback, waketime=NEVER):
        timer = FakeTimer(callback, waketime)
        self.timers.append(timer)
        return timer

    def unregister_timer(self, timer):
        if timer is not None:
            timer.cancelled = True
            if timer in self.timers:
                self.timers.remove(timer)

    def update_timer(self, timer, waketime):
        if timer is not None and not timer.cancelled:
            timer.waketime = waketime

    def register_callback(self, callback, waketime=NOW):
        self.callbacks.append((waketime, callback))
        callback(self.now)

    def advance(self, seconds):
        target = self.now + seconds
        while True:
            due = [
                t
                for t in self.timers
                if not t.cancelled and t.waketime <= target
            ]
            if not due:
                break
            due.sort(key=lambda t: t.waketime)
            timer = due[0]
            self.now = max(self.now, timer.waketime)
            res = timer.callback(self.now)
            if res == self.NEVER or res is None:
                timer.cancelled = True
                if timer in self.timers:
                    self.timers.remove(timer)
            else:
                timer.waketime = res
        self.now = target


class FakeGCodeCommand:
    def __init__(self, command, params, gcode):
        self._command = command
        self._params = dict(params)
        self._gcode = gcode

    def get(self, name, default="_SENTINEL"):
        if name in self._params:
            return self._params[name]
        if default != "_SENTINEL":
            return default
        raise GCodeError("Missing parameter '%s'" % (name,))

    def get_int(self, name, default="_SENTINEL"):
        val = self.get(name, default)
        if val is None:
            return None
        return int(val)

    def get_float(self, name, default="_SENTINEL"):
        val = self.get(name, default)
        if val is None:
            return None
        return float(val)

    def get_commandline(self):
        args = " ".join("%s=%s" % (k, v) for k, v in self._params.items())
        return ("%s %s" % (self._command, args)).strip()

    def respond_info(self, msg):
        self._gcode.info_messages.append(msg)

    def respond_raw(self, msg):
        self._gcode.raw_messages.append(msg)


class FakeGCode:
    error = GCodeError

    def __init__(self):
        self.commands = {}
        self.scripts = []
        self.raw_messages = []
        self.info_messages = []
        self._in_command = False

    def register_command(self, cmd, func, when_not_ready=False, desc=None):
        prev = self.commands.get(cmd)
        if func is None:
            self.commands.pop(cmd, None)
        else:
            self.commands[cmd] = func
        return prev

    def run_script(self, script):
        if self._in_command:
            raise RuntimeError(
                "Deadlock: run_script() called while gcode mutex is held; "
                "use run_script_from_command() instead (script=%r)" % (script,)
            )
        self._in_command = True
        try:
            self._execute_script(script)
        finally:
            self._in_command = False

    def run_script_from_command(self, script):
        self._execute_script(script)

    def _execute_script(self, script):
        self.scripts.append(script)
        for raw_line in script.splitlines():
            line = raw_line.strip()
            if not line or line.startswith(";"):
                continue
            self._dispatch_line(line)

    def _dispatch_line(self, line):
        tokens = shlex.split(line)
        if not tokens:
            return
        cmd = tokens[0].upper()
        params = {}
        for tok in tokens[1:]:
            if "=" in tok:
                k, v = tok.split("=", 1)
                params[k.upper()] = v
        handler = self.commands.get(cmd)
        if handler is not None:
            handler(FakeGCodeCommand(cmd, params, self))

    def run_command(self, cmd, **params):
        handler = self.commands[cmd.upper()]
        normalized = {k.upper(): str(v) for k, v in params.items()}
        was_in = self._in_command
        self._in_command = True
        try:
            return handler(FakeGCodeCommand(cmd.upper(), normalized, self))
        finally:
            self._in_command = was_in

    def respond_raw(self, msg):
        self.raw_messages.append(msg)

    def respond_info(self, msg):
        self.info_messages.append(msg)


class FakeHeater:
    def __init__(self, min_extrude_temp=170.0):
        self.min_extrude_temp = min_extrude_temp
        self.target_temp = 0.0


class FakeExtruder:
    def __init__(self, min_extrude_temp=170.0):
        self.heater = FakeHeater(min_extrude_temp=min_extrude_temp)

    def get_heater(self):
        return self.heater


class FakeToolhead:
    def __init__(self, homed_axes="", position=None):
        self.homed_axes = homed_axes
        self.position = position or [0.0, 0.0, 0.0, 0.0]

    def get_status(self, eventtime):
        return {
            "homed_axes": self.homed_axes,
            "position": list(self.position),
        }


class FakeGCodeMove:
    def __init__(self, absolute_extrude=True, absolute_coordinates=True):
        self.absolute_extrude = absolute_extrude
        self.absolute_coordinates = absolute_coordinates

    def get_status(self, eventtime):
        return {
            "absolute_extrude": self.absolute_extrude,
            "absolute_coordinates": self.absolute_coordinates,
        }


class FakePrintStats:
    def __init__(self, state="standby"):
        self.state = state

    def get_status(self, eventtime):
        return {"state": self.state}


class FakeVirtualSDCard:
    def __init__(self, sdcard_dirname):
        self.sdcard_dirname = sdcard_dirname
        self.started_files = []

    def cmd_SDCARD_PRINT_FILE(self, gcmd):
        self.started_files.append(gcmd.get("FILENAME", ""))


class FakeRunoutHelper:
    def __init__(self, filament_present=False):
        self.filament_present = filament_present
        self.native_calls = []

    def note_filament_present(self, eventtime, is_filament_present):
        self.filament_present = bool(is_filament_present)
        self.native_calls.append((eventtime, self.filament_present))


class FakeFilamentSensor:
    def __init__(self, filament_present=False):
        self.runout_helper = FakeRunoutHelper(filament_present=filament_present)


class FakeButtons:
    def __init__(self):
        self.registrations = []

    def register_buttons(self, pins, callback):
        self.registrations.append((list(pins), callback))

    def trigger(self, eventtime, state):
        for _, cb in self.registrations:
            cb(eventtime, state)


class FakeTemplate:
    def __init__(self, text):
        self.text = text

    def render(self, context=None):
        return self.text


class FakeGCodeMacro:
    def load_template(self, config, option, default=None):
        text = config.get(option, default or "")
        return FakeTemplate(text)


class FakeConfig:
    error = ConfigError

    def __init__(self, name, options, printer, sections=None):
        self._name = name
        self._options = dict(options)
        self._printer = printer
        self._sections = sections or {}

    def get_printer(self):
        return self._printer

    def get_name(self):
        return self._name

    def getsection(self, name):
        return self._sections[name]

    def get(self, option, default="_SENTINEL"):
        if option in self._options:
            return self._options[option]
        if default != "_SENTINEL":
            return default
        raise ConfigError("Missing option '%s' in [%s]" % (option, self._name))

    def getfloat(
        self, option, default="_SENTINEL", minval=None, maxval=None, above=None, below=None
    ):
        if option not in self._options:
            if default != "_SENTINEL":
                return default
            raise ConfigError("Missing float option '%s'" % (option,))
        val = float(self._options[option])
        if minval is not None and val < minval:
            raise ConfigError("Option '%s' below minval" % (option,))
        if above is not None and val <= above:
            raise ConfigError("Option '%s' not above" % (option,))
        if maxval is not None and val > maxval:
            raise ConfigError("Option '%s' above maxval" % (option,))
        if below is not None and val >= below:
            raise ConfigError("Option '%s' not below" % (option,))
        return val

    def getint(self, option, default="_SENTINEL", minval=None, maxval=None):
        if option not in self._options:
            if default != "_SENTINEL":
                return default
            raise ConfigError("Missing int option '%s'" % (option,))
        val = int(self._options[option])
        if minval is not None and val < minval:
            raise ConfigError("Option '%s' below minval" % (option,))
        if maxval is not None and val > maxval:
            raise ConfigError("Option '%s' above maxval" % (option,))
        return val

    def getboolean(self, option, default="_SENTINEL"):
        if option not in self._options:
            if default != "_SENTINEL":
                return default
            raise ConfigError("Missing bool option '%s'" % (option,))
        val = self._options[option]
        if isinstance(val, bool):
            return val
        return str(val).strip().lower() in ("1", "true", "yes", "on")


class FakePrinter:
    config_error = ConfigError

    def __init__(self, sdcard_dir):
        self.reactor = FakeReactor()
        self.gcode = FakeGCode()
        self.extruder = FakeExtruder()
        self.toolhead = FakeToolhead()
        self.gcode_move = FakeGCodeMove()
        self.print_stats = FakePrintStats()
        self.virtual_sdcard = FakeVirtualSDCard(sdcard_dir)
        self.buttons = FakeButtons()
        self.gcode_macro = FakeGCodeMacro()
        self.pause_calls = 0
        self.resume_calls = 0
        self.cancel_calls = 0

        self.objects = {
            "gcode": self.gcode,
            "extruder": self.extruder,
            "toolhead": self.toolhead,
            "gcode_move": self.gcode_move,
            "print_stats": self.print_stats,
            "virtual_sdcard": self.virtual_sdcard,
            "buttons": self.buttons,
            "gcode_macro": self.gcode_macro,
        }
        self.event_handlers = {}
        self._register_standard_commands()

    def _register_standard_commands(self):
        self.gcode.register_command(
            "SDCARD_PRINT_FILE", self.virtual_sdcard.cmd_SDCARD_PRINT_FILE
        )
        self.gcode.register_command("PAUSE", self._cmd_pause)
        self.gcode.register_command("RESUME", self._cmd_resume)
        self.gcode.register_command("CANCEL_PRINT", self._cmd_cancel_print)
        self.gcode.register_command("M82", self._cmd_m82)
        self.gcode.register_command("M83", self._cmd_m83)
        self.gcode.register_command("G90", self._cmd_g90)
        self.gcode.register_command("G91", self._cmd_g91)
        self.gcode.register_command("G28", self._cmd_g28)

    def _cmd_pause(self, gcmd):
        self.pause_calls += 1
        self.print_stats.state = "paused"

    def _cmd_resume(self, gcmd):
        self.resume_calls += 1
        self.print_stats.state = "printing"

    def _cmd_cancel_print(self, gcmd):
        self.cancel_calls += 1
        self.print_stats.state = "cancelled"

    def _cmd_m82(self, gcmd):
        self.gcode_move.absolute_extrude = True

    def _cmd_m83(self, gcmd):
        self.gcode_move.absolute_extrude = False

    def _cmd_g90(self, gcmd):
        self.gcode_move.absolute_coordinates = True

    def _cmd_g91(self, gcmd):
        self.gcode_move.absolute_coordinates = False

    def _cmd_g28(self, gcmd):
        self.toolhead.homed_axes = "xyz"

    def get_reactor(self):
        return self.reactor

    def lookup_object(self, name, default="_SENTINEL"):
        if name in self.objects:
            return self.objects[name]
        if default != "_SENTINEL":
            return default
        raise ConfigError("Object '%s' not found" % (name,))

    def load_object(self, config, name):
        if name in self.objects:
            return self.objects[name]
        if name == "prusa_kload":
            obj = prusa_kload.load_config(config.getsection("prusa_kload"))
            self.objects["prusa_kload"] = obj
            return obj
        raise ConfigError("Cannot load object '%s'" % (name,))

    def register_event_handler(self, event, callback):
        self.event_handlers.setdefault(event, []).append(callback)

    def send_event(self, event):
        for cb in self.event_handlers.get(event, []):
            cb()


@pytest.fixture
def klipper_env(tmp_path):
    sdcard_dir = tmp_path / "gcodes"
    sdcard_dir.mkdir()
    state_file = tmp_path / "state.json"

    def factory(
        options=None,
        material_sections=None,
        sensor_name="filament_switch_sensor fsensor",
        initial_present=False,
        standalone_pin=None,
        connect=True,
    ):
        printer = FakePrinter(str(sdcard_dir))
        cfg_opts = {
            "state_file": str(state_file),
            "debounce_delay": "1.0",
            "prompt_timeout": "300.0",
        }
        if standalone_pin is not None:
            cfg_opts["switch_pin"] = standalone_pin
        elif sensor_name is not None:
            sensor = FakeFilamentSensor(filament_present=initial_present)
            printer.objects[sensor_name] = sensor
            cfg_opts["filament_sensor"] = sensor_name

        if options:
            cfg_opts.update(options)

        sections = {}
        main_cfg = FakeConfig("prusa_kload", cfg_opts, printer, sections=sections)
        sections["prusa_kload"] = main_cfg

        kload = prusa_kload.load_config(main_cfg)
        printer.objects["prusa_kload"] = kload

        if material_sections:
            for mat_name, mat_opts in material_sections.items():
                sec_name = "prusa_kload_material %s" % (mat_name,)
                mat_cfg = FakeConfig(sec_name, mat_opts, printer, sections=sections)
                sections[sec_name] = mat_cfg
                prusa_kload_material.load_config_prefix(mat_cfg)

        if connect:
            printer.send_event("klippy:connect")
            printer.send_event("klippy:ready")

        sensor_obj = printer.objects.get(sensor_name) if sensor_name else None
        return printer, kload, sensor_obj, sdcard_dir, state_file

    return factory

