# Pre-flight Spoolman gate for the upload-and-print workflow.
#
# Orca's "Upload and Print" is a single Moonraker call, so Mainsail's start
# dialog -- the only place its Spoolman checks are mounted -- is never opened.
# This runs the equivalent checks on the printer instead, where the workflow
# cannot skip them, and aborts the job before anything heats.
#
# Fail-closed by operator decision on 2026-08-25: anything this cannot
# positively verify aborts. A Spoolman outage therefore stops printing. Set
# `enabled: False`, or pass BYPASS=1 for one job, when that is not wanted.

import json
import threading
import urllib.error
import urllib.parse
import urllib.request


class GuardResult:
    """The outcome of one pre-flight evaluation."""

    def __init__(self, ok, message):
        self.ok = ok
        self.message = message


def normalize_material(value):
    # Spoolman stores free text and slicers disagree on case and spacing:
    # "PLA+", "pla plus" and "PLA Plus" are one material to an operator.
    if value is None:
        return None
    text = "".join(ch for ch in str(value).lower() if ch.isalnum())
    return text or None


def evaluate(job_weight, job_material, spool_weight, spool_material,
             margin, check_material):
    """Decide whether a job may start. Pure; all I/O happens in the caller."""
    if job_weight is None:
        return GuardResult(False, "the G-code declares no filament weight")
    if spool_weight is None:
        return GuardResult(False, "the active spool reports no remaining weight")

    required = job_weight + margin
    if spool_weight < required:
        return GuardResult(
            False,
            "spool has %.1fg, job needs %.1fg%s"
            % (
                spool_weight,
                job_weight,
                "" if margin <= 0 else " plus %.1fg margin" % margin,
            ),
        )

    if check_material:
        job_norm = normalize_material(job_material)
        spool_norm = normalize_material(spool_material)
        if job_norm is None:
            return GuardResult(False, "the G-code declares no filament type")
        if spool_norm is None:
            return GuardResult(False, "the active spool declares no material")
        if job_norm != spool_norm:
            return GuardResult(
                False,
                "spool is %s, job needs %s" % (spool_material, job_material),
            )

    return GuardResult(
        True,
        "spool has %.1fg for a %.1fg job" % (spool_weight, job_weight),
    )


class SpoolGuard:
    cmd_SPOOL_GUARD_help = (
        "Abort before heating unless the active spool can finish this job"
    )

    def __init__(self, config):
        self.printer = config.get_printer()
        self.reactor = self.printer.get_reactor()
        self.gcode = self.printer.lookup_object("gcode")
        self.enabled = config.getboolean("enabled", True)
        self.moonraker_url = config.get(
            "moonraker_url", "http://127.0.0.1:7125"
        ).rstrip("/")
        self.timeout = config.getfloat("timeout", 8.0, above=0.0)
        self.margin = config.getfloat("margin", 0.0, minval=0.0)
        self.check_material = config.getboolean("check_material", True)
        self.gcode.register_command(
            "SPOOL_GUARD",
            self.cmd_SPOOL_GUARD,
            desc=self.cmd_SPOOL_GUARD_help,
        )

    def _get(self, path, body=None):
        url = self.moonraker_url + path
        data = None
        headers = {}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        request = urllib.request.Request(url, data=data, headers=headers)
        with urllib.request.urlopen(request, timeout=self.timeout) as response:
            return json.load(response).get("result")

    def _fetch(self, filename):
        # Moonraker is on loopback and answers in milliseconds, but a stalled
        # socket would freeze the whole Klipper reactor and with it the MCU
        # link. Do the blocking work on a thread and let the reactor breathe.
        box = {}

        def worker():
            # Name the stage before attempting it, so a failure reports which
            # of the three lookups broke rather than blaming the last one.
            try:
                box["stage"] = "read this job's metadata from Moonraker"
                quoted = urllib.parse.quote(filename)
                box["job"] = self._get(
                    "/server/files/metadata?filename=%s" % quoted
                )
                box["stage"] = "ask Moonraker which spool is active"
                active = self._get("/server/spoolman/spool_id") or {}
                spool_id = active.get("spool_id")
                box["spool_id"] = spool_id
                if spool_id is not None:
                    box["stage"] = "reach Spoolman through Moonraker"
                    box["spool"] = self._get(
                        "/server/spoolman/proxy",
                        {"request_method": "GET",
                         "path": "/v1/spool/%d" % int(spool_id)},
                    )
            except Exception as error:
                box["error"] = error

        thread = threading.Thread(target=worker)
        thread.daemon = True
        thread.start()
        deadline = self.reactor.monotonic() + self.timeout * 3 + 1.0
        eventtime = self.reactor.monotonic()
        while thread.is_alive() and eventtime < deadline:
            eventtime = self.reactor.pause(eventtime + 0.1)
        if thread.is_alive():
            raise self.gcode.error(
                "SPOOL_GUARD: Moonraker did not answer within %.0fs"
                % (self.timeout * 3 + 1.0)
            )
        if "error" in box:
            raise self.gcode.error(
                "SPOOL_GUARD: could not %s (%s). Fix it, or run "
                "SPOOL_GUARD BYPASS=1 to print unchecked."
                % (box.get("stage", "query Moonraker"), box["error"])
            )
        return box

    def cmd_SPOOL_GUARD(self, gcmd):
        if gcmd.get_int("BYPASS", 0):
            gcmd.respond_info("SPOOL_GUARD: bypassed by request")
            return
        if not self.enabled:
            gcmd.respond_info("SPOOL_GUARD: disabled in configuration")
            return

        # FILE= dry-runs the whole check against any uploaded job without
        # starting it. This is the only way to exercise the gate end to end,
        # because print_stats.filename is set only by an actual print.
        filename = gcmd.get("FILE", None)
        if not filename:
            print_stats = self.printer.lookup_object("print_stats", None)
            if print_stats is not None:
                status = print_stats.get_status(self.reactor.monotonic())
                filename = status.get("filename") or None
        if not filename:
            # Console tests and macro dry-runs have no job. There is nothing to
            # measure against, so this is not a spool failure.
            gcmd.respond_info("SPOOL_GUARD: no job loaded, nothing to check")
            return

        fetched = self._fetch(filename)
        if fetched.get("spool_id") is None:
            raise gcmd.error(
                "SPOOL_GUARD: no active spool selected, so this print cannot "
                "be tracked. Select one in Mainsail, or run "
                "SPOOL_GUARD BYPASS=1 to print unchecked."
            )

        job = fetched.get("job") or {}
        spool = fetched.get("spool") or {}
        result = evaluate(
            job.get("filament_weight_total"),
            job.get("filament_type"),
            spool.get("remaining_weight"),
            (spool.get("filament") or {}).get("material"),
            self.margin,
            self.check_material,
        )
        if not result.ok:
            raise gcmd.error(
                "SPOOL_GUARD: %s. Print from Mainsail's file list to confirm "
                "and proceed, or run SPOOL_GUARD BYPASS=1." % result.message
            )
        gcmd.respond_info("SPOOL_GUARD: %s" % result.message)

    def get_status(self, eventtime):
        # _PREFLIGHT tests for this object before issuing SPOOL_GUARD, so that
        # the published profiles still run on a printer without this extra.
        return {
            "enabled": self.enabled,
            "margin": self.margin,
            "check_material": self.check_material,
        }


def load_config(config):
    return SpoolGuard(config)
