# SV Zero machine-mode switch, as a Moonraker component.
#
# A job sliced for the other machine mode (standard or precision) is refused by
# SVZERO_REQUIRE on its first line, before any heater. With this component
# loaded the refusal calls the remote method `svzero_mode_switch`, and this:
#
#   1. waits for the refused job to end,
#   2. checks the printer is idle and cold: no job, no file active, nozzle and
#      bed targets 0,
#   3. enables svzero-mode-<mode>.cfg and parks any other mode file as .off
#      (standard mode is "no mode file"),
#   4. checks idle and cold AGAIN, immediately before the restart,
#   5. FIRMWARE_RESTARTs Klipper and waits for it, reverting the renames and
#      restarting again if Klipper does not come back ready,
#   6. confirms _SVZERO_PACK.mode now reads the requested mode, and
#   7. starts the same file again.
#
# It never restarts over a print, and it acts once per file per 15 minutes, so a
# job that keeps asking cannot ping-pong the printer. `SVZERO_MODE MODE=<name>`
# asks for a switch with no file, and then nothing is started.
#
# It is a Moonraker component, not a Klipper extra, because the restart it
# performs would end a Klipper extra halfway through. When Klipper becomes ready it sets
# _SVZERO_PACK.mode_switch, so installing it is the whole opt-in.
#
# INSTALL: copy this file to ~/moonraker/moonraker/components/, add
#
#     [svzero_mode_switch]
#
# to moonraker.conf, and restart Moonraker. Options, with their defaults:
#
#     mode_dir: svzero         # where the mode files are, relative to the config
#                              # directory; "." for a pack installed beside
#                              # printer.cfg
#     loop_window: 900         # seconds before the same file may switch again
#
# Its log is svzero_mode_switch.log in Moonraker's log directory.

import asyncio
import logging
import logging.handlers
import os
import re
import time

METHOD = "svzero_mode_switch"
INCLUDE = "[include svzero-mode-*.cfg]"
IDLE = {"standby", "complete", "cancelled", "error"}
JOB_END_WAIT = 60.0     # seconds for the refused job to finish
DOWN_WAIT = 20.0        # seconds to see the old Klipper go away
READY_WAIT = 120.0      # seconds for Klipper to come back
MODE_RE = re.compile(r"^[a-z][a-z0-9_]*$")
FILE_RE = re.compile(r"^svzero-mode-[a-z0-9_]+\.cfg$")


def plan(mode, mode_dir):
    """The renames that select `mode`, or raise ValueError with the reason."""
    names = os.listdir(mode_dir)
    on = sorted(n for n in names if FILE_RE.match(n))
    want = None if mode == "standard" else "svzero-mode-%s.cfg" % mode
    if want and want not in on and want + ".off" not in names:
        raise ValueError("no %s (or .off) in %s" % (want, mode_dir))
    moves = []
    for n in on:
        if n != want:
            if n + ".off" in names:
                raise ValueError("%s and %s.off both exist" % (n, n))
            moves.append((n, n + ".off"))
    if want and want not in on:
        moves.append((want + ".off", want))
    return moves


def rename_all(moves, mode_dir):
    for src, dst in moves:
        os.rename(os.path.join(mode_dir, src), os.path.join(mode_dir, dst))


def include_present(config_dir, mode_dir):
    """Is the mode include in printer.cfg or in a pack file beside the modes?"""
    paths = [os.path.join(config_dir, "printer.cfg")]
    paths += [os.path.join(mode_dir, n) for n in sorted(os.listdir(mode_dir))
              if n.endswith(".cfg") and not FILE_RE.match(n)]
    for path in paths:
        try:
            with open(path, encoding="utf-8") as f:
                if any(line.split("#", 1)[0].strip() == INCLUDE for line in f):
                    return True
        except OSError:
            continue
    return False


def gate(st):
    """Reasons NOT to restart Klipper now; empty means idle and cold."""
    if st.get("klippy") != "ready":
        return ["Klipper %s" % st.get("klippy")]
    why = []
    if st["job"] not in IDLE:
        why.append("job %s" % st["job"])
    if st["active"]:
        why.append("a file is active")
    if st["t_ext"] or st["t_bed"]:
        why.append("heaters on (nozzle %g, bed %g)" % (st["t_ext"], st["t_bed"]))
    return why


class SvzeroModeSwitch:
    def __init__(self, config):
        self.server = config.get_server()
        self.mode_dir_option = config.get("mode_dir", "svzero")
        self.loop_window = config.getfloat("loop_window", 900.0)
        self.busy = False
        self.last = {}
        self.log = logging.getLogger("svzero_mode_switch")
        self._log_ready = False
        self.server.register_remote_method(METHOD, self._on_request)
        self.server.register_event_handler(
            "server:klippy_ready", self._on_klippy_ready)

    # ------------------------------------------------------------ plumbing
    def _apis(self):
        return self.server.lookup_component("klippy_apis")

    def _dirs(self):
        fm = self.server.lookup_component("file_manager")
        config_dir = fm.get_directory("config")
        return config_dir, os.path.normpath(
            os.path.join(config_dir, self.mode_dir_option))

    def _open_log(self):
        if self._log_ready:
            return
        self._log_ready = True
        try:
            logs = self.server.lookup_component("file_manager").get_directory("logs")
            if logs:
                h = logging.handlers.RotatingFileHandler(
                    os.path.join(logs, "svzero_mode_switch.log"),
                    maxBytes=256 * 1024, backupCount=2)
                h.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
                self.log.addHandler(h)
        except Exception:
            logging.exception("svzero_mode_switch: no log file")

    async def _say(self, msg):
        self.log.info(msg)
        clean = re.sub(r'[;#*"]', "", msg)
        try:
            await self._apis().run_gcode(
                'RESPOND PREFIX="svzero" MSG="mode switch: %s"' % clean)
        except Exception:
            pass        # Klipper may be restarting; the log has it

    async def _snapshot(self):
        apis = self._apis()
        info = await apis.get_klippy_info(default=None)
        st = {"klippy": (info or {}).get("state", "disconnected")}
        if st["klippy"] != "ready":
            return st
        s = await apis.query_objects({
            "print_stats": ["state"], "virtual_sdcard": ["is_active"],
            "extruder": ["target"], "heater_bed": ["target"],
            "gcode_macro _SVZERO_PACK": ["mode"],
        })
        st.update(job=s["print_stats"]["state"],
                  active=s["virtual_sdcard"]["is_active"],
                  t_ext=s["extruder"]["target"], t_bed=s["heater_bed"]["target"],
                  mode=str(s.get("gcode_macro _SVZERO_PACK", {}).get("mode", "standard")).lower())
        return st

    async def _wait_restarted(self):
        """See the old Klipper go away, then wait for the new one."""
        apis = self._apis()
        end = time.monotonic() + DOWN_WAIT
        while time.monotonic() < end:
            info = await apis.get_klippy_info(default=None)
            if (info or {}).get("state") != "ready":
                break
            await asyncio.sleep(0.5)
        end = time.monotonic() + READY_WAIT
        while time.monotonic() < end:
            try:
                st = await self._snapshot()
                if st["klippy"] in ("ready", "error", "shutdown"):
                    return st
            except Exception:
                pass
            await asyncio.sleep(2)
        return {"klippy": "timeout"}

    # ------------------------------------------------------------- events
    async def _on_klippy_ready(self):
        self._open_log()
        try:
            objects = await self._apis().get_object_list(default=[])
            if "gcode_macro _SVZERO_PACK" in objects:
                await self._apis().run_gcode(
                    "SET_GCODE_VARIABLE MACRO=_SVZERO_PACK VARIABLE=mode_switch VALUE=True")
        except Exception as e:
            self.log.warning("could not enable mode_switch: %s", e)

    def _on_request(self, mode="", filename="", **unused):
        self._open_log()
        if self.busy:
            self.log.warning("request %r %r ignored, a switch is running", mode, filename)
            return
        self.busy = True
        task = asyncio.ensure_future(self._switch(str(mode), str(filename or "")))
        task.add_done_callback(lambda t: setattr(self, "busy", False))

    async def _switch(self, mode, filename):
        try:
            await self._do_switch(mode.strip().lower(), filename)
        except Exception:
            self.log.exception("switch to %r for %r failed", mode, filename)
            await self._say("failed with an exception, see svzero_mode_switch.log")

    async def _do_switch(self, mode, filename):
        self.log.info("request: mode=%s file=%r", mode, filename)
        config_dir, mode_dir = self._dirs()
        if not MODE_RE.match(mode):
            return await self._say("refused: %r is not a mode name" % mode)
        if filename and time.monotonic() - self.last.get(filename, -1e9) < self.loop_window:
            return await self._say("refused: %s already switched modes in the last %d min"
                                   % (filename, self.loop_window // 60))
        if not os.path.isdir(mode_dir):
            return await self._say("refused: no mode directory %s (mode_dir)" % mode_dir)
        if not include_present(config_dir, mode_dir):
            # No "*" on the console: it would end a RESPOND argument.
            self.log.info("no %s in printer.cfg or %s", INCLUDE, mode_dir)
            return await self._say("refused: neither printer.cfg nor the pack includes the mode files")
        try:
            moves = plan(mode, mode_dir)
        except ValueError as e:
            return await self._say("refused: %s" % e)

        end = time.monotonic() + JOB_END_WAIT
        st = await self._snapshot()
        while gate(st) and time.monotonic() < end:
            await asyncio.sleep(1)
            st = await self._snapshot()
        why = gate(st)
        if why:
            return await self._say("refused, printer not idle and cold: %s" % ", ".join(why))
        if st["mode"] == mode:
            return await self._say("already in %s mode, nothing to do" % mode)
        if not moves:
            return await self._say("refused: no file change selects %s mode" % mode)

        rename_all(moves, mode_dir)
        self.log.info("renamed: %s", moves)
        st = await self._snapshot()
        why = gate(st)
        if why:     # the last-moment check: something started meanwhile
            rename_all([(b, a) for a, b in reversed(moves)], mode_dir)
            return await self._say("aborted before restart, printer busy: %s. Files restored."
                                   % ", ".join(why))
        await self._say("%s mode to %s mode, restarting Klipper" % (st["mode"], mode))
        await self._apis().do_restart("FIRMWARE_RESTART")
        st = await self._wait_restarted()
        if st.get("klippy") != "ready":
            rename_all([(b, a) for a, b in reversed(moves)], mode_dir)
            self.log.error("Klipper %s after restart; renames reverted", st.get("klippy"))
            await self._apis().do_restart("FIRMWARE_RESTART")
            back = await self._wait_restarted()
            return await self._say("Klipper came back %s in %s mode. Files restored, "
                                   "restarted again (%s). Job not started."
                                   % (st.get("klippy"), mode, back.get("klippy")))
        if st["mode"] != mode:
            return await self._say("Klipper reports %s mode after the restart, not %s. "
                                   "Job not started." % (st["mode"], mode))
        cfg = await self._apis().query_objects({"configfile": ["settings"]})
        steps = cfg["configfile"]["settings"].get("stepper_x", {}).get("microsteps")
        if not filename:
            return await self._say("now in %s mode (stepper_x microsteps %s)" % (mode, steps))
        self.last[filename] = time.monotonic()
        await self._apis().start_print(filename)
        await self._say("now in %s mode (microsteps %s), started %s again"
                        % (mode, steps, filename))


def load_component(config):
    return SvzeroModeSwitch(config)
