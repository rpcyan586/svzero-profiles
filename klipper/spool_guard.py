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
        # THE ACTIVE SPOOL, LATCHED AT PRINT START. SPOOL_GUARD already fetches
        # this to compare against the job; publishing what it learned costs one
        # dictionary and saves every downstream consumer from opening its own
        # Moonraker connection. END_PRINT's cooldown ramp is the first: it needs
        # the material to pick a descent policy, and it runs hours later, long
        # after any HTTP call would be appropriate from inside a macro.
        #
        # RAM, deliberately, and never persisted. A saved material does not mean
        # "this print used it", it means "some print did, possibly three jobs
        # ago" -- the same reasoning that keeps _COOL_STATE volatile. It dies
        # with a Klipper restart and a consumer that finds it empty falls back.
        self._spool = {"id": None, "material": None, "name": None,
                       "vendor": None, "color_hex": None, "at": None}
        self.gcode.register_command(
            "SPOOL_GUARD",
            self.cmd_SPOOL_GUARD,
            desc=self.cmd_SPOOL_GUARD_help,
        )

    def _announce_no_spoolman(self):
        """Say it once per session, then stay quiet.

        This doubles as the only advertisement the feature gets. Somebody who
        never configured Spoolman still learns, once, that the gate exists and
        what it would do for them -- which is better than a silently inert
        section nobody ever reads.
        """
        if getattr(self, "_said_no_spoolman", False):
            return
        self._said_no_spoolman = True
        self.gcode.respond_info(
            "spool_guard: Spoolman is not configured on this Moonraker, so "
            "spool checks are skipped. Configure [spoolman] in moonraker.conf "
            "to have jobs checked for enough filament and the right material "
            "before any heater is commanded."
        )

    def _fetch(self, filename, live=None):
        client = moonraker.MoonrakerClient(
            self.reactor, self.moonraker_url, self.timeout
        )
        quoted = client.quote(filename)
        job_key = "read this job's metadata from Moonraker"
        spool_key = "ask Moonraker which spool is active"
        meta_call = (job_key, "/server/files/metadata?filename=%s" % quoted, None)
        try:
            got = client.run([
                (spool_key, "/server/spoolman/spool_id", None),
                meta_call,
            ])
        except moonraker.MoonrakerError as error:
            # SPOOLMAN IS SIMPLY NOT SET UP HERE, which is most installations.
            # Moonraker answers 404 because it never loaded the spoolman
            # component, not because anything is broken, and refusing every
            # print over that would make the pack unusable for anyone who does
            # not run Spoolman. A 404 on that endpoint is an ABSENT FEATURE; a
            # timeout or a 500 is a broken one, and those still refuse.
            if error.status == 404 and error.key == spool_key:
                self._announce_no_spoolman()
                # Distinguish absent integration from a configured integration
                # with no active spool. The latter must still refuse.
                return {"spoolman_available": False}
            else:
                raise self._refuse(
                    "could not %s" % error,
                    "fix it, or SPOOL_GUARD BYPASS=1", live
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
        if fetched.get("spoolman_available") is False:
            if not rehearsal:
                self._spool = {key: None for key in self._spool}
            return
        if fetched.get("spool_id") is None:
            raise self._refuse(
                "no active spool selected, so this print cannot be tracked",
                "select one in Mainsail, or SPOOL_GUARD BYPASS=1",
                live,
            )

        job = fetched.get("job") or {}
        spool = fetched.get("spool") or {}
        # Latched before the verdict, and only for a real print. A FILE= dry run
        # must not overwrite the live spool -- rehearsing a check against some
        # other job would otherwise hand the cooldown ramp that job's material.
        if not rehearsal:
            filament = spool.get("filament") or {}
            self._spool = {
                "id": fetched.get("spool_id"),
                "material": normalize_material(filament.get("material")) or None,
                "name": filament.get("name"),
                "vendor": (filament.get("vendor") or {}).get("name"),
                "color_hex": filament.get("color_hex"),
                "at": self.reactor.monotonic(),
            }
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
            # Flattened rather than nested: Klipper status dictionaries are read
            # from Jinja, where printer.spool_guard.material is a great deal
            # easier to get right than a subscript into a nested dict.
            "spool_id": self._spool["id"],
            "material": self._spool["material"],
            "filament_name": self._spool["name"],
            "vendor": self._spool["vendor"],
            "color_hex": self._spool["color_hex"],
        }


def load_config(config):
    return SpoolGuard(config)
