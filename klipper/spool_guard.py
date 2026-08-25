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

from . import moonraker


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
    # Klipper reports a raised gcode error once for the failing command and
    # again for the macro that invoked it, so anything inside the exception is
    # printed twice. Explanations go out as ordinary responses, which are not
    # repeated, and the error itself stays to one short line.
    REFUSED = "SPOOL_GUARD refused this job"

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
        # A refusal arms a one-shot override for that exact job. Starting the
        # same file again inside this window is the deliberate second act that
        # says "I know, print it anyway". Mainsail's own warning cannot serve
        # that purpose: it is advisory, and both it and an Orca upload-and-print
        # reach Klipper as the same SDCARD_PRINT_FILE, so the printer cannot
        # tell which one asked.
        self.override_window = config.getfloat(
            "override_window", 300.0, minval=0.0
        )
        self._armed_file = None
        self._armed_until = 0.0
        self._armed_reason = ""
        self.gcode.register_command(
            "SPOOL_GUARD",
            self.cmd_SPOOL_GUARD,
            desc=self.cmd_SPOOL_GUARD_help,
        )

    def _fetch(self, filename, live=None):
        client = moonraker.MoonrakerClient(
            self.reactor, self.moonraker_url, self.timeout
        )
        quoted = client.quote(filename)
        try:
            got = client.run([
                ("read this job's metadata from Moonraker",
                 "/server/files/metadata?filename=%s" % quoted, None),
                ("ask Moonraker which spool is active",
                 "/server/spoolman/spool_id", None),
            ])
        except moonraker.MoonrakerError as error:
            raise self._refuse(
                "could not %s" % error, "fix it, or SPOOL_GUARD BYPASS=1", live
            )
        job = got["read this job's metadata from Moonraker"]
        active = got["ask Moonraker which spool is active"] or {}
        spool_id = active.get("spool_id")
        spool = None
        if spool_id is not None:
            try:
                spool = client.run([(
                    "reach Spoolman through Moonraker",
                    "/server/spoolman/proxy",
                    {"request_method": "GET",
                     "path": "/v1/spool/%d" % int(spool_id)},
                )])["reach Spoolman through Moonraker"]
            except moonraker.MoonrakerError as error:
                raise self._refuse(
                    "could not %s" % error,
                    "fix it, or SPOOL_GUARD BYPASS=1", live,
                )
        return {"job": job, "spool_id": spool_id, "spool": spool}

    def _refuse(self, reason, remedy, filename=None):
        """Explain on one line, then fail with a line cheap to see twice.

        `filename` arms the retry override. A FILE= dry run passes None, so
        rehearsing a refusal can never authorize a later real print.
        """
        if filename is not None and self.override_window > 0.0:
            self._armed_file = filename
            self._armed_until = self.reactor.monotonic() + self.override_window
            self._armed_reason = reason
            remedy = "start it again within %.0f min to print anyway" % (
                self.override_window / 60.0
            )
        self.gcode.respond_info("SPOOL_GUARD: %s -- %s" % (reason, remedy))
        return self.gcode.error(self.REFUSED)

    def _take_override(self, filename):
        """Consume a previously armed override for this exact job."""
        if filename is None or self._armed_file != filename:
            return None
        if self.reactor.monotonic() > self._armed_until:
            return None
        reason = self._armed_reason
        self._armed_file = None
        self._armed_reason = ""
        return reason

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
        rehearsal = gcmd.get("FILE", None)
        filename = rehearsal
        if not filename:
            print_stats = self.printer.lookup_object("print_stats", None)
            if print_stats is not None:
                status = print_stats.get_status(self.reactor.monotonic())
                filename = status.get("filename") or None
        # Only a real print may arm or consume an override.
        live = None if rehearsal else filename
        if live:
            overridden = self._take_override(live)
            if overridden is not None:
                gcmd.respond_info(
                    "SPOOL_GUARD: overriding -- %s" % overridden
                )
                return
        if not filename:
            # Console tests and macro dry-runs have no job. There is nothing to
            # measure against, so this is not a spool failure.
            gcmd.respond_info("SPOOL_GUARD: no job loaded, nothing to check")
            return

        fetched = self._fetch(filename, live)
        if fetched.get("spool_id") is None:
            raise self._refuse(
                "no active spool selected, so this print cannot be tracked",
                "select one in Mainsail, or SPOOL_GUARD BYPASS=1",
                live,
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
            raise self._refuse(
                result.message,
                "or SPOOL_GUARD BYPASS=1",
                live,
            )
        gcmd.respond_info("SPOOL_GUARD: %s" % result.message)

    def get_status(self, eventtime):
        # _PREFLIGHT tests for this object before issuing SPOOL_GUARD, so that
        # the published profiles still run on a printer without this extra.
        return {
            "enabled": self.enabled,
            "margin": self.margin,
            "check_material": self.check_material,
            "armed_file": self._armed_file,
            "armed_seconds": max(
                0.0, self._armed_until - self.reactor.monotonic()
            ) if self._armed_file else 0.0,
        }


def load_config(config):
    return SpoolGuard(config)
