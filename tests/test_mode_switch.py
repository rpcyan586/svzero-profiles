"""The svzero_mode_switch Moonraker component, against fakes of Moonraker.

No Moonraker is imported: the component uses duck typing, so these fakes stand
in for the server, klippy_apis and file_manager with the same call shapes.
"""
import asyncio
import importlib.util
import os
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "svzero_mode_switch", ROOT / "moonraker" / "svzero_mode_switch.py")
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


class Printer:
    """Klipper as the component sees it. A restart re-reads the mode files."""

    def __init__(self, mode_dir):
        self.mode_dir = mode_dir
        self.state = "ready"
        self.job, self.active, self.t_ext, self.t_bed = "error", False, 0.0, 0.0
        self.restarts, self.started, self.scripts = 0, [], []
        self.fail_restart = False          # come back "error" once
        self.ignore_mode = False           # come back ready in the old mode
        self.busy_on_query = None          # job state to report from query n
        self.queries = 0
        self.mode = self._mode_from_files()

    def _mode_from_files(self):
        on = [n for n in os.listdir(self.mode_dir) if M.FILE_RE.match(n)]
        return on[0][len("svzero-mode-"):-len(".cfg")] if on else "standard"


class Apis:
    def __init__(self, printer):
        self.p = printer
        self._down = False

    async def get_klippy_info(self, default=None):
        if self._down:                     # one "startup" reading per restart
            self._down = False
            return {"state": "startup"}
        return {"state": self.p.state}

    async def query_objects(self, objects):
        if "configfile" in objects:
            steps = 64 if self.p.mode == "precision" else 16
            return {"configfile": {"settings": {"stepper_x": {"microsteps": steps}}}}
        self.p.queries += 1
        job = self.p.job
        if self.p.busy_on_query and self.p.queries >= self.p.busy_on_query:
            job = "printing"
        return {"print_stats": {"state": job},
                "virtual_sdcard": {"is_active": job == "printing"},
                "extruder": {"target": self.p.t_ext},
                "heater_bed": {"target": self.p.t_bed},
                "gcode_macro _SVZERO_PACK": {"mode": self.p.mode}}

    async def get_object_list(self, default=None):
        return ["gcode_macro _SVZERO_PACK"]

    async def run_gcode(self, script):
        self.p.scripts.append(script)
        return "ok"

    async def do_restart(self, gc):
        assert gc == "FIRMWARE_RESTART"
        self.p.restarts += 1
        self._down = True
        if self.p.fail_restart and self.p.restarts == 1:
            self.p.state = "error"
        else:
            self.p.state = "ready"
            if not self.p.ignore_mode:
                self.p.mode = self.p._mode_from_files()
        self.p.job = "standby"
        return "ok"

    async def start_print(self, filename):
        self.p.started.append(filename)
        return "ok"


class FileManager:
    def __init__(self, config_dir, logs_dir):
        self.dirs = {"config": config_dir, "logs": logs_dir}

    def get_directory(self, root):
        return self.dirs[root]


class Server:
    def __init__(self, components):
        self.components = components
        self.remote, self.events = {}, {}

    def register_remote_method(self, name, cb):
        self.remote[name] = cb

    def register_event_handler(self, name, cb):
        self.events[name] = cb

    def lookup_component(self, name):
        return self.components[name]


class Config:
    def __init__(self, server, values=None):
        self.server, self.values = server, values or {}

    def get_server(self):
        return self.server

    def get(self, name, default=None):
        return self.values.get(name, default)

    def getfloat(self, name, default=None):
        return float(self.values.get(name, default))


class ModeSwitchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.config_dir = Path(self.tmp.name) / "config"
        self.pack = self.config_dir / "svzero"
        self.pack.mkdir(parents=True)
        (self.config_dir / "printer.cfg").write_text("[include svzero/svzero-1.3.7.cfg]\n")
        (self.pack / "svzero-1.3.7.cfg").write_text("[include svzero_pack.cfg]\n" + M.INCLUDE + "\n")
        (self.pack / "svzero-mode-precision.cfg.off").write_text("")
        logs = Path(self.tmp.name) / "logs"
        logs.mkdir()
        self.printer = Printer(str(self.pack))
        server = Server({"klippy_apis": Apis(self.printer),
                         "file_manager": FileManager(str(self.config_dir), str(logs))})
        self.comp = M.load_component(Config(server))
        self.server = server

    def tearDown(self):
        for h in list(self.comp.log.handlers):
            self.comp.log.removeHandler(h)
            h.close()
        self.tmp.cleanup()

    def switch(self, mode, filename="job.gcode"):
        asyncio.run(self.comp._do_switch(mode, filename))

    def said(self):
        return " | ".join(s for s in self.printer.scripts if s.startswith("RESPOND"))

    def files(self):
        return sorted(n for n in os.listdir(self.pack) if n.startswith("svzero-mode-"))

    def test_registers_the_method_and_the_ready_hook(self):
        self.assertIn(M.METHOD, self.server.remote)
        self.assertIn("server:klippy_ready", self.server.events)

    def test_switches_restarts_and_starts_the_job_again(self):
        self.switch("precision")
        self.assertEqual(self.files(), ["svzero-mode-precision.cfg"])
        self.assertEqual(self.printer.restarts, 1)
        self.assertEqual(self.printer.started, ["job.gcode"])
        self.assertIn("now in precision mode (microsteps 64), started job.gcode again", self.said())

    def test_switches_back_to_standard(self):
        self.switch("precision", "a.gcode")
        self.switch("standard", "b.gcode")
        self.assertEqual(self.files(), ["svzero-mode-precision.cfg.off"])
        self.assertEqual(self.printer.mode, "standard")
        self.assertEqual(self.printer.started, ["a.gcode", "b.gcode"])

    def test_refuses_with_heaters_on_and_changes_nothing(self):
        self.printer.t_bed = 100.0
        self.comp_wait = M.JOB_END_WAIT
        M.JOB_END_WAIT = 0.0
        try:
            self.switch("precision")
        finally:
            M.JOB_END_WAIT = self.comp_wait
        self.assertEqual(self.files(), ["svzero-mode-precision.cfg.off"])
        self.assertEqual(self.printer.restarts, 0)
        self.assertIn("refused, printer not idle and cold: heaters on", self.said())

    def test_the_same_file_cannot_switch_twice_in_the_window(self):
        self.switch("precision")
        self.switch("standard")
        self.assertEqual(self.printer.restarts, 1)
        self.assertIn("already switched modes in the last 15 min", self.said())

    def test_refuses_without_the_mode_include(self):
        (self.pack / "svzero-1.3.7.cfg").write_text("[include svzero_pack.cfg]\n")
        self.switch("precision")
        self.assertEqual(self.printer.restarts, 0)
        self.assertIn("neither printer.cfg nor the pack includes the mode files", self.said())

    def test_refuses_an_unknown_mode(self):
        self.switch("turbo")
        self.assertEqual(self.printer.restarts, 0)
        self.assertIn("no svzero-mode-turbo.cfg", self.said())

    def test_restores_the_files_if_a_job_starts_before_the_restart(self):
        # Idle at the first check, printing by the last-moment one.
        self.printer.busy_on_query = 2
        self.switch("precision")
        self.assertEqual(self.files(), ["svzero-mode-precision.cfg.off"])
        self.assertEqual(self.printer.restarts, 0)
        self.assertIn("aborted before restart, printer busy", self.said())

    def test_reverts_and_restarts_again_if_klipper_fails_to_come_back(self):
        self.printer.fail_restart = True
        self.switch("precision")
        self.assertEqual(self.files(), ["svzero-mode-precision.cfg.off"])
        self.assertEqual(self.printer.restarts, 2)
        self.assertEqual(self.printer.started, [])
        self.assertIn("Files restored", self.said())

    def test_does_not_start_the_job_if_the_mode_did_not_change(self):
        self.printer.ignore_mode = True
        self.switch("precision")
        self.assertEqual(self.printer.started, [])
        self.assertIn("reports standard mode after the restart", self.said())

    def test_a_switch_with_no_file_starts_nothing(self):
        self.switch("precision", "")
        self.assertEqual(self.printer.restarts, 1)
        self.assertEqual(self.printer.started, [])
        self.assertIn("now in precision mode (stepper_x microsteps 64)", self.said())

    def test_klipper_ready_turns_switching_on(self):
        asyncio.run(self.comp._on_klippy_ready())
        self.assertIn("SET_GCODE_VARIABLE MACRO=_SVZERO_PACK VARIABLE=mode_switch VALUE=True",
                      self.printer.scripts)

    def test_console_lines_carry_no_respond_breaking_characters(self):
        self.switch("../etc")
        for line in self.printer.scripts:
            if line.startswith("RESPOND"):
                body = line.split('MSG="', 1)[1][:-1]
                self.assertFalse(set(body) & set(';#*"'), line)


if __name__ == "__main__":
    unittest.main()
