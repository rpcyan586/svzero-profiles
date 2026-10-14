#!/usr/bin/env python3
"""Parse AND render every macro in a Klipper .cfg, in Klipper's own environment.

    ./tools/check-macros.py klipper/config/nozzle_brush.cfg klipper/config/purge_line.cfg

Two lessons are baked in, both learned the expensive way:

* **Klipper's environment, exactly.** jinja2.Environment('{%', '%}', '{', '}')
  with NO extensions (gcode_macro.py:74). An earlier harness enabled
  jinja2.ext.do, accepted a {% do %} tag Klipper does not have, and the resulting
  config HALTED THE PRINTER. A validator that is more permissive than the target
  converts a syntax error into confidence.

* **Run it with the printer's own interpreter, not a convenient one.** The same
  lesson, one layer out. Zero2's klippy-env has jinja2 **2.11.3**; the comparison host has
  **3.1.2**, and the workstation has none. Rendering on the comparison host is a fine fast
  pre-check while the printer is mid-print, but it is NOT the gate: 3.x accepts
  syntax and semantics 2.11 does not. Validate with
  `/home/sovol/klippy-env/bin/python check-macros.py` on the target before
  deploying, every time.

  "The workstation has none" is not the same as "no local pre-check". A venv
  pinned to the printer's own Jinja is stricter than the comparison host ever was, and does not
  need the comparison host to be up:

      python3 -m venv j211
      ./j211/bin/pip install "jinja2==2.11.3" "markupsafe==1.1.1"

  2.11.3 imports fine on Python 3.14. Still not klippy-env — that is 3.9 — so
  the on-target run before deploying stays mandatory.

* **Assert which branch a scenario reached.** Jinja evaluates only the branch it
  takes, so a scenario that quietly lands in the wrong one renders clean and
  proves nothing: the branch it was written FOR is still unrendered. That is the
  _PURGE_STATUS lesson below, one level up, and it was live here — a
  _CHAMBER_GUARD scenario spent its whole existence rendering an empty macro
  body, because the macro sits behind `running == 1` and the scenario never set
  it. The `expect` / `forbid` tables name a string the output must or must not
  contain. A new scenario without an entry may be testing nothing.

* **No ";", "#" or "*" inside a RESPOND argument.** Klipper's extended-command
  regex ends the argument list at any of them (gcode.py extended_r, args group
  "[^#*;]*?"), so the closing quote is never seen and the whole command dies with
  "Malformed command". This has now bitten twice, most recently on a message
  explaining a formula as "b + k * n^2". Checked here because the rendered text
  is the only place it is visible.

* **Render, do not just parse.** Parsing catches syntax; it does not catch a
  variable referenced before it is set, or one left behind by a rename. Both have
  shipped from this repo -- _PURGE_STATUS kept referencing a removed `volume`
  through several commits because only PURGE_LINE was ever rendered.

Missing printer state resolves to Undefined rather than raising, matching how
Klipper behaves with `|default(...)`. Anything else is StrictUndefined, so a
genuine typo fails loudly.
"""
import ast, configparser, math, os, re, shlex, sys, jinja2


class Mock(dict):
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:                     # Jinja turns this into Undefined
            raise AttributeError(k)


def box(a, b, c, d):
    return [[a, b], [c, b], [c, d], [a, d]]


def traditional_gcode(name):
    """Match Klipper's GCodeDispatch.is_traditional_gcode classification."""
    return len(name) >= 2 and name[0].isupper() and name[1].isdigit()


# gcode.py's own splitter, used for TRADITIONAL commands only.
ARGS_R = re.compile('([A-Z_]+|[A-Z*/])')


def klipper_params(name, rawparams):
    """Build `params` exactly as Klipper's dispatcher would for this command.

    This matters, and modelling it by hand did not work. A traditional name --
    a letter then a digit, so M191 but not _CHAMBER_ON -- never receives
    extended KEY=VALUE parsing. _process_commands splits the line on ARGS_R and
    keeps whatever text follows each key, so `STIR=1` becomes the string "=1"
    and "=1"|float is 0.0. Fixtures that supplied a clean "1" tested an input
    Klipper cannot produce, and a whole class of silently-zero arguments passed
    validation for weeks. Derive the params instead of asserting them.
    """
    if not traditional_gcode(name):
        pairs = [arg.split('=', 1) for arg in shlex.split(rawparams)]
        return Mock({p[0].upper(): p[1] for p in pairs if len(p) == 2})
    parts = ARGS_R.split(("%s %s" % (name, rawparams)).upper())
    got = {parts[i]: parts[i + 1].strip() for i in range(1, len(parts) - 1, 2)}
    got.pop(name[0], None)          # the command's own letter, e.g. M -> "191"
    return Mock(got)


def check(path):
    env = jinja2.Environment('{%', '%}', '{', '}', undefined=jinja2.StrictUndefined)
    env.globals["action_raise_error"] = lambda m: (_ for _ in ()).throw(RuntimeError(m))
    # Klipper's other action_* callables. They return a string that is emitted
    # into the rendered G-code, so model them as producing nothing.
    for _fn in ("action_respond_info", "action_emergency_stop"):
        env.globals[_fn] = lambda *a, **k: ""
    # A remote call happens at render time and leaves nothing in the output, and
    # a refusal raises right after it, so record the calls to assert on them.
    remote_calls = []
    env.globals["action_call_remote_method"] = (
        lambda method, **kw: remote_calls.append(
            "%s %s" % (method, " ".join("%s=%s" % i for i in sorted(kw.items())))) or "")

    def remote_ok(s, label, suffix):
        if (s, label) not in remote:
            return True
        want = remote[(s, label)]
        if want is None and remote_calls:
            print("   FAIL    %s%s -> remote call it must not make: %s"
                  % (s, suffix, remote_calls[0]))
            return False
        if want is not None and want not in remote_calls:
            print("   FAIL    %s%s -> no remote call %r, got %r"
                  % (s, suffix, want, remote_calls))
            return False
        return True
    cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=('#', ';'))
    cp.read_string('\n'.join(l.split('#')[0] for l in open(path, encoding="utf-8")))

    # Every gcode_macro, not only the underscore-prefixed state holders. Klipper
    # exposes variable_* on all of them, so restricting this to _NAME made the
    # validator LESS faithful than the target: a macro reading its own variable
    # rendered Undefined here and worked on the printer, which is the wrong way
    # round for a gate.
    settings = {s: Mock({k[9:]: ast.literal_eval(v.strip())
                         for k, v in cp.items(s) if k.startswith("variable_")})
                for s in cp.sections() if s.startswith("gcode_macro ")}
    # A macro that budgets current has to read the strip's present state, so
    # every [neopixel] section in the file gets an object with a dark chain of
    # its own declared length. Derived from the file rather than hard-coded, so
    # this keeps working for the next strip.
    for _s in cp.sections():
        if _s.startswith("neopixel "):
            _n = cp.getint(_s, "chain_count", fallback=1)
            settings.setdefault(_s, Mock(color_data=[[0.0, 0.0, 0.0, 0.0]] * _n))
    # Shared macro files may depend on a small hardware-identity include that is
    # validated separately. Supply the same public interface here so the common
    # controller can still be rendered as a standalone source file.
    settings.setdefault("gcode_macro _CH_HW", Mock(fan="exhaust_fan"))
    # The standalone fixtures default to 1.4.x. A consolidated 1.3.7 config
    # supplies its real hardware identity: use that name in mocks and expected
    # fan commands too, so the assembled pack is checked without changing it.
    exhaust_fan = settings["gcode_macro _CH_HW"].fan

    def expected_tokens(value):
        values = [value] if isinstance(value, str) else value
        return [v.replace("exhaust_fan SPEED=", exhaust_fan + " SPEED=")
                 .replace("FAN=exhaust_fan", "FAN=" + exhaust_fan) for v in values]

    # Sovol's own objects, always present at runtime but defined in Macro.cfg
    # rather than in any file we ship. Overriding macros legitimately read them,
    # so supply the same public interface here instead of failing the render.
    settings.setdefault("gcode_macro _global_var",
                        Mock(z_maximum_lifting_distance=155,
                             filament_sensor_print=False,
                             load_filament_extruder_temp=250,
                             cancel_park=Mock(x=10.0, y=10.0, z=10.0, e=2.0)))
    # PURGE_LINE reads _BRUSH to absorb the scrub retract and end-of-print debt.
    # _BRUSH is defined in nozzle_brush.cfg; when purge_line.cfg is checked
    # standalone the mock must supply the variables PURGE_LINE touches.
    # START_PRINT also reads it, for the pad centre it parks over during the
    # final heat-up, so the pad geometry belongs in the same mock. A second
    # setdefault for the same key is silently ignored -- which is how the pad
    # values went missing on the first attempt.
    settings.setdefault("gcode_macro _BRUSH",
                        Mock(retracted=3.0, owed_max=10.0, retract_f=2700,
                             pad_x_min=-10.0, pad_x_max=-7.3,
                             pad_y_min=5.5, pad_y_max=39.0))
    # cooldown.cfg is optional, so chamber_fan.cfg, end_print.cfg and
    # cancel_print.cfg all reach it through `'gcode_macro _COOL_STATE' in
    # printer`. Supply it here so those three render their ramp-aware branch
    # rather than only the not-installed one.
    settings.setdefault("gcode_macro _COOL_STATE",
                        Mock(active=0, phase=1, policy_set=0, p_final=35.0,
                             p_chamber_rate=1.5, p_bed_rate=5.0,
                             c0=60.0, b0=110.0, c_cmd=60.0, b_cmd=110.0,
                             c_step=1.0, b_step=3.0, interval=40.0,
                             has_heater=0, elapsed=0, settle_elapsed=0,
                             c_rate=0.83, b_rate=2.5, total_s=1800.0,
                             last_report=0,
                             idle_restore=1800.0, coast_warned=0,
                             material="default", rates_set=0, assist=0,
                             assist_curtain=0.0, assist_exhaust=0.0,
                             ch_written=32.0))
    settings.setdefault("gcode_macro _COOLDOWN_CFG",
                        Mock(final=35.0, chamber_rate=1.5, bed_rate=2.0,
                             chamber_step=1.0, deadband=2.0, settle_min=180,
                             settle_max=600, settle_slope=0.5, settle_tick=5,
                             park_z=100.0, max_seconds=5400, coast_delta=5.0,
                             override_delta=0.5, idle_margin=300,
                             ramp_tick=15, report_seconds=60, eta_window=240))
    # cooldown.cfg reads the chamber loop's own state -- `running` to decide
    # whether it must restart the loop, `chamber_slope` to gate the settle.
    # print_bed_target defaults to the STRING "None" on the real machine -- the
    # reset path writes that literal -- so the fixture uses it too. START_PRINT
    # has to survive it without a |float, which is the whole reason its guard
    # tests the string rather than relying on a numeric default.
    settings.setdefault("gcode_macro _CH_STATE",
                        Mock(running=1, chamber_slope=0.1,
                             print_bed_target="None"))
    printer = Mock(settings)
    printer.update({
        "exclude_object": Mock(objects=[Mock(polygon=box(60, 60, 90, 90))]),
        "toolhead": Mock(
            max_accel=5000.0, homed_axes="xyz", position=[0, 0, 0, 0],
            estimated_print_time=123.456,
            axis_minimum=Mock(x=0.0, y=0.0, z=0.0),
            axis_maximum=Mock(x=200.0, y=200.0, z=250.0),
        ),
        "save_variables": Mock(variables=Mock(was_interrupted=0.0,
                                         nb_lane=0, ch_target=32.0, ch_kp=0.15,
                                              end_retract=0.0)),
        "filament_switch_sensor filament_sensor": Mock(enabled=True,
                                                       filament_detected=True),
        # _COOLDOWN_START reads idle_timeout.timeout to know what to restore.
        "configfile": Mock(settings=Mock(extruder=Mock(min_extrude_temp=150),
                                         idle_timeout=Mock(timeout=1800.0),
                                         # END_PRINT and CANCEL_PRINT restore it.
                                         printer=Mock(square_corner_velocity=5.0,
                                                      max_accel=40000.0,
                                                      max_velocity=1200.0))),
        "gcode_move": Mock(absolute_extrude=False, absolute_coordinates=True,
                           gcode_position=Mock(x=0.0, y=0.0, z=0.42),
                           position=Mock(x=0.0, y=0.0, z=0.42)),
        "extruder": Mock(temperature=25.0, target=0.0, power=0.0),
        "heater_bed": Mock(temperature=25.0, target=65.0),
        "temperature_sensor chamber_temp": Mock(temperature=30.0),
        "temperature_sensor Toolhead_Temp": Mock(temperature=30.0),
        "fan_generic " + exhaust_fan: Mock(speed=0.0, rpm=1500.0),
        # total_duration: _BRUSH_MARK_DONE subtracts it from the stamp
        # CLEAN_NOZZLE took at its start, to report how long the wipe ran.
        "print_stats": Mock(state="standby", filename="job.gcode",
                            total_duration=900.0,
                            info=Mock(current_layer=None)),
    })
    params = Mock(
        VOL_MAX="21", FIL_D="1.75", NOZZLE="0.4", CLEAR="4", CORNER="1",
        S="40",
    )

    bad = 0
    for s in cp.sections():
        if s.startswith("gcode_macro ") and cp.has_option(s, "rename_existing"):
            alias = s.split()[1].upper()
            renamed = cp.get(s, "rename_existing").strip().upper()
            if traditional_gcode(alias) != traditional_gcode(renamed):
                bad += 1
                print(
                    "   FAIL    %s -> rename_existing changes G-code type (%s to %s)"
                    % (s, alias, renamed)
                )
                continue
        if not cp.has_option(s, 'gcode') or not cp.get(s, 'gcode').strip():
            continue
        scenarios = [("", params, "S40", {}, {})]
        if s == "gcode_macro CLEAN_NOZZLE":
            scenarios = [
                ("heated", Mock(), "", {}, {}),
                ("cold-contact", Mock(HEAT="0"), "HEAT=0", {}, {}),
                ("dry", Mock(DRY="1"), "DRY=1", {}, {}),
            ]
        elif s == "gcode_macro CHAMBER_ORBIT_SWEEP":
            scenarios = [("default", Mock(CYCLES="1"), "CYCLES=1", {}, {})]
        elif s == "gcode_macro _BRUSH_MARK_DONE":
            # Both branches. The shipped default for t_start is -1.0, so
            # WITHOUT a patch only the "not measurable" path ever renders and
            # the arithmetic that matters is never executed. total_duration is
            # 900 in the fixture, so a stamp of 800 must report 100.0s.
            scenarios = [
                ("measured", params, "", {}, {},
                 {"gcode_macro _BRUSH": Mock(t_start=800.0)}),
                ("outside-print", params, "", {}, {}, {}),
                # A wipe SLOWER than the running minimum must not move it. The
                # lead is bounded by the shortest wipe, so a ratchet that ever
                # rose would license a lead the machine cannot honour.
                ("not-shortest", params, "", {}, {},
                 {"gcode_macro _BRUSH": Mock(t_start=800.0),
                  "save_variables": Mock(variables=Mock(wipe_seconds_min=60.0))}),
            ]
        elif s == "gcode_macro _PREFLIGHT":
            # The optional start-of-job hook (2026-10-13): called only when a
            # machine defines it; the pack itself defines none.
            scenarios = [
                ("no-hook", None, "", {}, {}, {"gcode_macro _SVZERO_PRINT_START_HOOK": None}),
                ("hook", None, "", {}, {}, {"gcode_macro _SVZERO_PRINT_START_HOOK": Mock()}),
            ]
        elif s == "gcode_macro SET_CLEAN_TEMP":
            # The table moved out of the slicer's G-code on 2026-09-05, so the
            # lookup is now ours to get right rather than the emitter's.
            # The bed at its target, so the lookup is what is tested; the
            # bed-moving hold has its own scenario below.
            settled = {"heater_bed": Mock(temperature=100.0, target=100.0)}
            scenarios = [
                ("exact", Mock(MATERIAL="ABS"), "MATERIAL=ABS", {}, {}, settled),
                # Prefix match over `order`: "PLA+" must land on pla.
                ("prefix", Mock(MATERIAL="PLA+"), "MATERIAL=PLA+", {}, {}, settled),
                # Unknown material takes Sovol's flat 200, not a guess.
                ("fallback", Mock(MATERIAL="Nylon"), "MATERIAL=Nylon", {}, {}, settled),
                ("explicit", Mock(MATERIAL="ABS", TEMP="150"),
                 "MATERIAL=ABS TEMP=150", {}, {}, settled),
                # The plate still coming down from a preheat boost: the nozzle
                # waits at probe_temp, not at the wipe temperature (2026-10-13).
                ("bed-coming-down", Mock(MATERIAL="ABS"), "MATERIAL=ABS", {}, {},
                 {"heater_bed": Mock(temperature=118.0, target=100.0)}),
            ]
        elif s == "gcode_macro START_PRINT":
            scenarios = [
                # 1.3.7: a load cell exists, so the tuned overlay path runs.
                ("load-cell", params, "", {}, {},
                 {"probe_pressure": Mock(last_z_result=0.0)}),
                # 1.4.x: eddy only. METHOD must be omitted entirely so the
                # extra's own "already calibrated" skip can fire.
                ("eddy-only", params, "", {}, {},
                 {"probe_pressure": None}),
                ("camera-tuner", params, "", {}, {},
                 {"probe_pressure": Mock(last_z_result=0.0),
                  "camera_tune": Mock(active=False)}),
            ]
        elif s == "gcode_macro ORBIT_WHINE_SWEEP":
            scenarios = [
                ("default", params, "", {}, {}),
                # 300 mm/s at r=20 needs 4500 mm/s2, just inside the 5000 limit;
                # r=10 would need 9000 and must be refused rather than attempted.
                ("over-accel", Mock(R="10", VMIN="300", VMAX="300"),
                 "R=10 VMIN=300 VMAX=300", {}, {}),
            ]
        elif s == "gcode_macro _CHAMBER_ORBIT_CYCLE":
            scenarios = [
                ("full", params, "", {}, {},
                 {"gcode_macro _CHAMBER_ORBIT_STATE": Mock(dict(
                     arcs=[[-14.0, -18.0, 2, 101], [22.0, 26.0, 2, 104],
                           [-58.0, -58.0, 2, 141], [14.0, 10.0, 2, 101]],
                     cx=100.0, cy=100.0, feed=6000, active=1, index=0))}),
                ("inactive", params, "", {}, {},
                 {"gcode_macro _CHAMBER_ORBIT_STATE": Mock(dict(
                     arcs=[[-14.0, -18.0, 2, 101]], cx=100.0, cy=100.0, feed=6000,
                     active=0, index=0))}),
            ]
        elif s == "gcode_macro _CHAMBER_ORBIT_STEP":
            # A PRINTER PATCH, not a state override: the 4th element patches the
            # macro's OWN variables and this macro has none -- it reads
            # _CHAMBER_ORBIT_STATE. Same trap as _SVZERO_CHECK_PROFILE.
            arcs = [[-14.0, -18.0, 2, 101], [22.0, 26.0, 2, 104],
                    [-22.0, -18.0, 2, 104], [14.0, 10.0, 2, 101]]
            def orbit(**kw):
                d = dict(arcs=arcs, cx=100.0, cy=100.0, feed=699, active=1,
                         index=0)
                d.update(kw)
                return {"gcode_macro _CHAMBER_ORBIT_STATE": Mock(d)}
            scenarios = [
                ("mid-sweep", params, "", {}, {}, orbit(index=1)),
                # Past the last arc: re-centre and restart, never park outside.
                ("wraps", params, "", {}, {}, orbit(index=4)),
                ("inactive", params, "", {}, {}, orbit(active=0)),
            ]
        elif s == "gcode_macro _BRUSH_INFER_CLEAN_TEMP":
            sv = lambda **kw: {"save_variables": Mock(variables=Mock(**kw))}
            scenarios = [
                # The spool knows: reuse the table rather than the curve.
                ("from-spool", params, "", {}, {},
                 dict(sv(last_nozzle_temp=270.0),
                      **{"spool_guard": Mock(material="ABS")})),
                # No spool, but this machine printed at 270 last time:
                # 20 + 2/3*250 = 186.7 -> up to 5 -> 190.
                ("from-last-print", params, "", {}, {},
                 sv(last_nozzle_temp=270.0)),
                # Nothing at all: the one genuinely blind job.
                ("blind", params, "", {}, {}, sv(last_nozzle_temp=0.0)),
            ]
        elif s == "gcode_macro SVZERO_REQUIRE":
            # mode=None leaves the attribute off entirely: a pack section from
            # before modes existed, which must read as standard.
            def pack(seen=0, mode=None, switch=None, steps=None, have=None):
                fields = dict(version="1.0.4", seen=seen)
                if mode is not None:
                    fields["mode"] = mode
                if switch is not None:
                    fields["mode_switch"] = switch
                if steps is not None:
                    fields["mode_microsteps"] = steps
                out = {"gcode_macro _SVZERO_PACK": Mock(fields),
                       "print_stats": Mock(filename="job.gcode")}
                if have is not None:
                    out["configfile"] = Mock(settings=Mock(dict(
                        printer.configfile.settings,
                        stepper_x=Mock(microsteps=have))))
                return out
            scenarios = [
                # Profiles from before modes: VERSION only.
                ("version-match", Mock(VERSION="1.0.4"), "VERSION=1.0.4",
                 {}, {}, pack()),
                ("version-mismatch", Mock(VERSION="0.9.0"), "VERSION=0.9.0",
                 {}, {}, pack()),
                ("mode-match", Mock(VERSION="1.0.4", MODE="standard"),
                 "VERSION=1.0.4 MODE=standard", {}, {}, pack(mode="standard")),
                ("mode-match-precision", Mock(VERSION="1.0.4", MODE="precision"),
                 "VERSION=1.0.4 MODE=precision", {}, {}, pack(mode="precision")),
                ("mode-match-case", Mock(VERSION="1.0.4", MODE="PRECISION"),
                 "VERSION=1.0.4 MODE=PRECISION", {}, {}, pack(mode="precision")),
                # The version still only warns when the mode agrees.
                ("version-mismatch-mode-match", Mock(VERSION="0.9.0", MODE="standard"),
                 "VERSION=0.9.0 MODE=standard", {}, {}, pack(mode="standard")),
                # Refused in both directions, and a job that names no mode is
                # a standard job.
                ("precision-job-standard-printer", Mock(VERSION="1.0.4", MODE="precision"),
                 "VERSION=1.0.4 MODE=precision", {}, {}, pack(mode="standard")),
                ("standard-job-precision-printer", Mock(VERSION="1.0.4", MODE="standard"),
                 "VERSION=1.0.4 MODE=standard", {}, {}, pack(mode="precision")),
                ("legacy-job-precision-printer", Mock(VERSION="1.0.4"), "VERSION=1.0.4",
                 {}, {}, pack(mode="precision")),
                # With switching on, the refusal hands the job to the helper.
                ("precision-job-standard-printer-switch", None, "VERSION=1.0.4 MODE=precision",
                 {}, {}, pack(mode="standard", switch=True)),
                ("standard-job-precision-printer-switch", None, "VERSION=1.0.4 MODE=standard",
                 {}, {}, pack(mode="precision", switch=True)),
                ("mode-match-switch", None, "VERSION=1.0.4 MODE=precision",
                 {}, {}, pack(mode="precision", switch=True)),
                # Without the component the refusal names the rename, both ways.
                ("manual-into-precision", None, "VERSION=1.0.4 MODE=precision",
                 {}, {}, pack(mode="standard", switch=False)),
                ("manual-back-to-standard", None, "VERSION=1.0.4 MODE=standard",
                 {}, {}, pack(mode="precision", switch=False, steps=64, have=64)),
                # A mode file that did not take effect: printer.cfg's own
                # stepper sections came after the pack and won.
                ("precision-not-applied", None, "VERSION=1.0.4 MODE=precision",
                 {}, {}, pack(mode="precision", steps=64, have=16)),
                ("precision-applied", None, "VERSION=1.0.4 MODE=precision",
                 {}, {}, pack(mode="precision", steps=64, have=64)),
            ]
        elif s == "gcode_macro SVZERO_MODE":
            def pack(mode, switch):
                return {"gcode_macro _SVZERO_PACK": Mock(
                    version="1.0.4", seen=0, mode=mode, mode_switch=switch)}
            scenarios = [
                ("report", None, "", {}, {}, pack("standard", True)),
                ("already", None, "MODE=standard", {}, {}, pack("standard", True)),
                ("off", None, "MODE=precision", {}, {}, pack("standard", False)),
                ("ask", None, "MODE=PRECISION", {}, {}, pack("standard", True)),
            ]
        elif s == "gcode_macro _SVZERO_CHECK_PROFILE":
            def pack(seen, mode=None):
                fields = dict(version="1.0.4", seen=seen)
                if mode is not None:
                    fields["mode"] = mode
                return {"gcode_macro _SVZERO_PACK": Mock(fields)}
            scenarios = [
                ("no-handshake", params, "", {}, {}, pack(0)),
                ("handshake-seen", params, "", {}, {}, pack(1)),
                ("no-handshake-standard", params, "", {}, {}, pack(0, "standard")),
                # Outside standard mode an unidentified job is refused.
                ("no-handshake-precision", params, "", {}, {}, pack(0, "precision")),
                ("handshake-seen-precision", params, "", {}, {}, pack(1, "precision")),
            ]
        elif s == "gcode_macro _CH_PROXY_ANCHOR":
            scenarios = [("valid", params, "", {"proxy_valid": 1}, {})]
        elif s == "gcode_macro _CH_PROXY_INVALIDATE":
            scenarios = [
                ("anchored", Mock(REASON="1"), "REASON=1",
                 {"proxy_valid": 1, "proxy_anchor": 41.2,
                  "stock_anchor": 45.4}, {}),
                ("missing-anchor", Mock(REASON="2"), "REASON=2",
                 {"proxy_valid": 1, "proxy_anchor": None,
                  "stock_anchor": None}, {}),
            ]
        elif s == "gcode_macro _CHAMBER_LOOP":
            scenarios = [
                ("stock", params, "", {}, {}),
                ("anchored", params, "",
                 {"observer_source": 2, "proxy_valid": 0,
                  "proxy_anchor": 41.2, "stock_anchor": 45.4,
                  "preheat_phase": 5}, {}),
                ("power-handoff", params, "",
                 {"observer_source": 1, "proxy_valid": 1,
                  "proxy_anchor": 41.2, "stock_anchor": 45.4},
                 {"power": 0.5}),
            ]
        # These pass None for params: klipper_params derives them from the raw
        # command line below, the way the dispatcher actually would.
        if s == "gcode_macro M141":
            scenarios = [
                ("standalone", None, "S32", {}, {}),
                ("post-minimum", None, "S33",
                 {"preheat_target_pending": 1}, {}),
                # Orca Target 0. This has to be distinguishable at
                # START_PRINT from a profile that never mentioned the chamber,
                # which is what control_declined carries.
                ("declined", None, "S0", {}, {}),
                # Orca appends this AFTER machine_end_gcode, so it lands while a
                # cooldown ramp is live. It must be deferred, not applied: a
                # 43-minute ABS ramp lost its chamber leg to exactly this.
                ("ramp-owns-it", None, "S0", {}, {},
                 {"gcode_macro _COOL_STATE": Mock(active=1)}),
            ]
        elif s == "gcode_macro M191":
            scenarios = [
                ("default-compatibility", None, "", {}, {},
                 {"chamber_preheat": Mock()}),
                ("exact-minimum", None, "S30", {}, {},
                 {"chamber_preheat": Mock()}),
                # The real published line. Every KEY=VALUE here reaches the
                # macro with a literal "=" still attached.
                ("shadow-test-line", None,
                 "S33 Z15 STIR=1 BOOST=1 MAX_BOOST=10 COAST_RATE=1"
                 " INITIAL_BOOST=5 FILTER_ALPHA=0.25",
                 {}, {}, {"chamber_preheat": Mock()}),
                ("without-python", None, "S30", {}, {},
                 {"chamber_preheat": None}),
                ("abort", None, "S0", {}, {}),
                # The wipe-lead line has two branches, learned and configured,
                # and neither renders without a configured lead.
                ("wipe-lead-learned", None, "S45", {}, {},
                 {"chamber_preheat": Mock(),
                  "configfile": Mock(settings=Mock(dict(
                      printer.configfile.settings,
                      chamber_preheat=Mock(wipe_lead_seconds=70.0)))),
                  "save_variables": Mock(variables=Mock(dict(
                      printer.save_variables.variables,
                      # 60 s, a round figure and no machine's: 0.8 of it is
                      # 48, the lead the formula's own comment works through.
                      **{"wipe_seconds_min": 60.0, "wipe_lead_autotune": 1})))}),
                ("wipe-lead-configured", None, "S45", {}, {},
                 {"chamber_preheat": Mock(),
                  "configfile": Mock(settings=Mock(dict(
                      printer.configfile.settings,
                      chamber_preheat=Mock(wipe_lead_seconds=70.0))))}),
                # Marlin's R: heats like S when the chamber is below, waits for
                # cooling when it is above, and loses to S when both are given.
                ("r-heating", None, "R45", {}, {},
                 {"chamber_preheat": Mock(),
                  "temperature_sensor chamber_temp": Mock(temperature=30.0)}),
                ("r-cooling", None, "R35", {}, {},
                 {"chamber_preheat": Mock(),
                  "temperature_sensor chamber_temp": Mock(temperature=48.0)}),
                ("r-cooling-without-python", None, "R35", {}, {},
                 {"chamber_preheat": None,
                  "temperature_sensor chamber_temp": Mock(temperature=48.0)}),
                ("s-beats-r", None, "S45 R35", {}, {},
                 {"chamber_preheat": Mock(),
                  "temperature_sensor chamber_temp": Mock(temperature=48.0)}),
                ("tol-ignored", None, "S45 TOL=5", {}, {},
                 {"chamber_preheat": Mock()}),
                # Bed boost is the default; BOOST=0 is the way out.
                ("boost-off", None, "S30 BOOST=0", {}, {},
                 {"chamber_preheat": Mock()}),
            ]

        # cooldown.cfg carries two actuator models and four phases, and an
        # unrendered branch is an unvalidated one -- the _PURGE_STATUS lesson in
        # the docstring above. These scenarios patch the `printer` object
        # itself, which no earlier scenario needed: the chamber heater's
        # presence IS the branch, and _COOL_STATE has to be replaced wholesale
        # to walk the phases.
        heater = {"heater_generic chamber_heater": Mock(temperature=58.0,
                                                        target=60.0)}
        hot_bed = {"heater_bed": Mock(temperature=108.0, target=110.0)}

        # THE SPLIT'S ARITHMETIC IN DUTY, ON PURPOSE (2026-10-13). The assist
        # now blends in each fan's rpm with a measured k(Z); these scenarios
        # test the split, the latch and the integral, whose numbers were
        # written for k = 1 and fans linear in duty. An identity curve and a
        # flat strength table give exactly that, so their expectations still
        # name the branch they were written for. The rpm and k(Z) behaviour
        # has its own scenarios, against the shipped curves and fit (assist-rpm-*).
        LINEAR_FANS = dict(exhaust_w0=1.0, exhaust_w_per_mm=0.0, curtain_w_far=1.0,
                           curtain_w_near=0.0, curtain_w_mm=1.0, strength_duty=1.0,
                           exhaust_curve_duty=[0.0, 1.0], exhaust_curve_rpm=[0.0, 1.0],
                           curtain_curve_duty=[0.0, 1.0], curtain_curve_rpm=[0.0, 1.0])

        def th_at(z):
            """The base toolhead with the plate at a given height."""
            return Mock(max_accel=5000.0, homed_axes="", position=[0, 0, z, 0],
                        estimated_print_time=123.456,
                        axis_minimum=Mock(x=0.0, y=0.0, z=0.0),
                        axis_maximum=Mock(x=200.0, y=200.0, z=250.0))

        def cool(ch=None, bed_temp=None, bed_power=0.0, **kw):
            """A _COOL_STATE mid-ramp on the PC policy: 110/60 to 35 at 2.5.

            ch_target is kept consistent with c_cmd by default, because
            _COOLDOWN_TICK aborts on a mismatch -- so a scenario that left the
            base mock's 32 C in place would silently test only the abort path
            and never render the ramp. Pass `ch` to test the mismatch on
            purpose.
            """
            # Rates and total, as _COOLDOWN_START would derive them for this
            # policy: cd 25 and bd 75 over t_total = max(25/1.0, 75/2.5) = 30
            # min, so rc = 0.8333 and rb = 2.5 and both axes land together.
            base = dict(active=1, phase=2, policy_set=1, p_final=35.0,
                        p_chamber_rate=1.0, p_bed_rate=2.5,
                        c0=60.0, b0=110.0, c_cmd=60.0, b_cmd=110.0,
                        c_step=1.0, b_step=3.0, interval=72.0,
                        c_rate=25.0 / 30.0, b_rate=2.5, total_s=1800.0,
                        last_report=0,
                        has_heater=0, elapsed=640, settle_elapsed=180,
                        idle_restore=1800.0, coast_warned=0,
                        material="pc", rates_set=0, assist=0,
                        assist_curtain=0.0, assist_exhaust=0.0,
                        curtain_latched=0, idle_set=0.0,
                        assist_latched=0, prev_err=0.0, overrun_s=0.0,
                        assist_i=0.0,
                        eta_bed=0.0, eta_at=0.0, eta_bed_b=0.0, eta_at_b=0.0,
                        meas_rate=0.0, eta_last=0.0, eta_last_at=0.0,
                        heat_sum=0.0, heat_n=0,
                        ch_written=0.0)
            # `b_at` places the scenario on the tick that commands this bed
            # setpoint, by solving the schedule for elapsed. Assist cases care
            # about where the bed is RELATIVE to its commanded value, and since
            # the commanded value is now a function of the clock, setting it
            # directly would describe an impossible state. The line is put a
            # quarter-degree above b_at -- the target leads the line (2026-10-13)
            # -- so with the shipped lead of 0.25 the assist's error against its
            # aim is exactly bed - b_at.
            b_at = kw.pop("b_at", None)
            shipped_tick = kw.pop("shipped_tick", False)
            base.update(kw)
            if b_at is not None:
                base["elapsed"] = int(round(base["settle_elapsed"]
                                            + (base["b0"] - b_at - 0.25) / base["b_rate"] * 60.0))
            # THE COMMANDED PAIR IS DERIVED FROM ELAPSED, not carried
            # independently. Since 2026-09-05 the macro computes its setpoints
            # as c0 - rate * t, so a scenario that set elapsed and c_cmd to
            # unrelated values would describe a state the ramp can never be in
            # -- and, worse, would still render. Anything the scenario passes
            # explicitly still wins, which is how the override cases work.
            # The ramp time is THIS tick's, 0 on the first (2026-10-13); what
            # the ramp last commanded is the previous tick's target, which
            # leads its line: the next degree down once the line reaches one.
            _ramp = max(0, base["elapsed"] - base["settle_elapsed"]) if base["phase"] == 2 else 0
            if "c_cmd" not in kw:
                base["c_cmd"] = float(max(base["p_final"], round(base["c0"] - base["c_rate"] * _ramp / 60.0)))
            if "b_cmd" not in kw:
                _prev = _ramp - 15
                _line = base["b0"] - base["b_rate"] * _prev / 60.0
                base["b_cmd"] = (float(base["b0"]) if base["phase"] != 2 or _prev < 0
                                 else float(base["p_final"]) if _line <= base["p_final"] + 1
                                 else float(max(base["p_final"], math.ceil(_line - 1e-6) - 1)))
            if "last_report" not in kw:
                base["last_report"] = _ramp
            if ch is None:
                ch = base["c_cmd"] + (2.0 if base["has_heater"] else 0.0)
            # ch_written tracks ch_target for the same reason ch_target tracks
            # c_cmd: the override check now compares against what the ramp last
            # WROTE, so a scenario that left them apart would test the abort
            # path and never render the ramp.
            if "ch_written" not in kw:
                base["ch_written"] = ch
            # The assist scenarios' arithmetic is written against a 0.13 exhaust
            # floor, which was the default until 2026-10-04; pinned so they keep
            # testing the arithmetic rather than the published default (0.15).
            # The degree already reported is the one the plate is on, so a
            # scenario says a progress line only when it asks for one, as
            # last_report did for the per-minute lines.
            if "last_degree" not in kw:
                base["last_degree"] = int((base["b_cmd"] + 8.0 if bed_temp is None else bed_temp) // 1)
            patch = {"gcode_macro _COOL_STATE": Mock(base),
                     "save_variables": Mock(variables=Mock(ch_target=ch, ch_minfan=0.13)),
                     # .get: chamber_fan.cfg's scenarios use cool() too, and
                     # checked on its own it has no cooldown.cfg loaded.
                     # curtain_enable 0: these scenarios were written for the
                     # exhaust alone, the shipped default until 2.1.0. Those
                     # that want the curtain say so; the assist-rpm ones run
                     # the shipped settings as they are.
                     "gcode_macro _COOLDOWN_ASSIST_CFG":
                         Mock(dict(settings.get("gcode_macro _COOLDOWN_ASSIST_CFG") or {}, **LINEAR_FANS,
                                   curtain_enable=0))}
            # THE TICK HELD AT 15 s. Since 2026-10-13 the tick is ramp_tick / rate,
            # 6 s at this fixture's 2.5 C/min, but these scenarios' integral and
            # b_cmd arithmetic is written per 15 s tick. ramp_tick is scaled so
            # it stays 15 s here; the ramp-tick-* scenarios take the shipped
            # setting (shipped_tick=True) and test the scaling itself.
            if not shipped_tick and base["b_rate"] > 0:
                patch["gcode_macro _COOLDOWN_CFG"] = Mock(dict(
                    settings.get("gcode_macro _COOLDOWN_CFG") or {},
                    ramp_tick=15.0 * base["b_rate"], ramp_tick_min=2.0,
                    ramp_tick_max=max(30.0, 15.0 * base["b_rate"])))
            # The bed the ramp believes it commanded, coasting a little above
            # it. A fixed 110 here would trip the bed-override abort in every
            # scenario whose b_cmd has moved on. Scenarios that want the
            # mismatch, or a specific bed temperature, still pass heater_bed.
            # power defaults to 0: a plate above its schedule has a heater at
            # zero, and the assist's run-ahead gate reads it.
            patch["heater_bed"] = Mock(
                temperature=base["b_cmd"] + 8.0 if bed_temp is None else bed_temp,
                target=base["b_cmd"], power=bed_power)
            return patch

        if s == "gcode_macro _COOLDOWN_START":
            scenarios = [
                ("heaterless", params, "", {}, {}, hot_bed),
                ("heater", params, "", {}, {}, dict(hot_bed, **heater)),
                # An explicit chamber rate is reported and ignored: the cooldown
                # does not control the chamber (2026-09-14).
                ("chamber-rate-ignored", params, "", {}, {},
                 dict(hot_bed, **{"gcode_macro _COOL_STATE":
                                  Mock(material="abs", policy_set=1, rates_set=1,
                                       p_final=35.0, p_chamber_rate=1.0,
                                       p_bed_rate=2.5)})),
                # Chamber already at the endpoint: bed-only, no chamber leg.
                ("bed-only", params, "", {}, {},
                 dict(hot_bed, **{"save_variables":
                                  Mock(variables=Mock(ch_target=30.0))})),
                # Nothing above the endpoint at all -- must turn heaters off
                # rather than schedule a zero-length ramp.
                ("nothing-to-do", params, "", {}, {},
                 {"heater_bed": Mock(temperature=25.0, target=0.0),
                  "save_variables": Mock(variables=Mock(ch_target=0.0))}),
            ]
        elif s == "gcode_macro _CHAMBER_VENT":
            # This supervisor had NO scenarios, so its branches were never
            # rendered. It is also the macro that can drive the exhaust to 1.0.
            hot = {"extruder": Mock(temperature=250.0, target=250.0, power=0.5),
                   "fan_generic " + exhaust_fan: Mock(speed=0.0, rpm=0.0)}
            def vent(floor, **kw):
                sv = {"was_interrupted": 0.0, "nb_lane": 0, "ch_target": 32.0,
                      "ch_vent_floor": floor, "ch_stall_rpm": 500,
                      "ch_stall_samples": 3, "ch_start_grace": 2}
                sv.update(kw)
                return dict(hot, **{"save_variables": Mock(variables=Mock(sv))})
            scenarios = [
                # A real floor, chamber PI not running: it must command the fan.
                ("floor-set-runs", None, "", {"running": 0, "vent_latched": 1}, {},
                 vent(0.11)),
                # ZERO floor: must NOT latch and must NOT command a speed. If it
                # latched at zero duty the tach guard would count a fan that is
                # off by design as a stall and fail safe at 1.0.
                ("floor-zero-is-off", None, "", {"running": 0, "vent_latched": 1}, {},
                 vent(0.0)),
                # The rounding IS the deadband: below half a percent is off.
                ("floor-rounds-to-off", None, "", {"running": 0, "vent_latched": 1}, {},
                 vent(0.004)),
                # A COOLDOWN RAMP PAST SETTLE OWNS THE EXHAUST. A real floor and
                # a hot nozzle, but the timer must write nothing -- neither the
                # floor nor the release-to-zero.
                ("ramp-owns-exhaust", None, "",
                 {"running": 0, "vent_latched": 1, "vent_active": 1}, {},
                 dict(vent(0.11), **{"gcode_macro _COOL_STATE": Mock(active=1, phase=2)})),
                # ...but during SETTLE the print's state is left alone, so the
                # same floor still runs.
                ("settle-leaves-vent", None, "", {"running": 0, "vent_latched": 1}, {},
                 dict(vent(0.11), **{"gcode_macro _COOL_STATE": Mock(active=1, phase=1)})),
            ]

        elif s == "gcode_macro _CH_ADAPT_BEGIN":
            # Published defaults since 2026-10-04: nothing saved means the
            # known-good 0.15 floor and no trial. The trial branch is opt-in.
            def adapt(**kw):
                sv = {"ch_target": 32.0}
                sv.update(kw)
                return {"save_variables": Mock(variables=Mock(sv))}
            scenarios = [
                ("published-defaults", None, "", {}, {}, adapt()),
                ("opt-in-trial", None, "", {}, {},
                 adapt(ch_floor_autotune=1, ch_minfan=0.14)),
            ]
        elif s == "gcode_macro _COOLDOWN_CLEAR":
            # KEEP_POLICY is the whole point of this macro having a parameter,
            # and it is only reachable by passing one. Rendered with the default
            # it proves nothing about the branch that matters.
            scenarios = [("clears-policy", None, "", {}, {}),
                         ("keeps-policy", None, "KEEP_POLICY=1", {}, {})]

        elif s == "gcode_macro _COOLDOWN_TICK":
            scenarios = [
                ("inactive", params, "", {}, {}, cool(active=0)),
                # SETTLE ENDS WHEN THE NOZZLE HAS COOLED TO THE BED (2026-09-14).
                # The bed sits at b_cmd + 8 = 118 C in these fixtures, and the
                # extruder patch sets the nozzle -- the base mock's 25 C would
                # end every settle on its first tick.
                ("settle-hold", params, "", {}, {"temperature": 250.0},
                 cool(phase=1, settle_elapsed=0, elapsed=0)),
                ("settle-nozzle-cooled", params, "", {}, {"temperature": 95.0},
                 cool(phase=1, settle_elapsed=60, elapsed=60)),
                # The settle phase used to say NOTHING for up to ten minutes,
                # which read as a hung cooldown. A nozzle still hotter than the
                # bed holds it open, so this must report rather than fall silent.
                ("settle-progress", params, "", {}, {"temperature": 180.0},
                 cool(phase=1, settle_elapsed=300, elapsed=300)),
                # The backstop: still hot at the cap, the ramp starts anyway and
                # names the temperature it stopped on.
                ("settle-cap", params, "", {}, {"temperature": 250.0},
                 cool(phase=1, settle_elapsed=600, elapsed=600)),
                ("ramp-heaterless", params, "", {}, {}, cool()),
                # Whole-degree commands, and a progress line with an ETA.
                ("ramp-integer", params, "", {}, {}, cool()),
                # ONE LINE PER DEGREE (2026-10-12). The plate at 99.0 C last
                # reported 101, so 100 and 99 each get a line. 110 C at the
                # anchor 460 s ago is 1.435 C/min measured, 43.9 min to 36 C;
                # that is longer than the 22.3 min of schedule left plus the
                # 15 min grace, so the figure is 2240 s, 37 min. The anchors
                # roll, and the heater's duty since the last line rides along.
                ("ramp-progress", params, "", {}, {},
                 cool(last_degree=101, eta_bed=110.0, bed_temp=99.0)),
                # On the degree already reported: nothing said.
                ("ramp-same-degree", params, "", {}, {},
                 cool(last_degree=99, eta_bed=110.0, bed_temp=99.0)),
                # Smoothed: 1500 s said 15 s ago carries as 1485, and the line
                # moves 30 % of the way to the new 2240 -- 1711.5 s, 29 min.
                ("ramp-progress-smoothed", params, "", {}, {},
                 cool(last_degree=100, eta_bed=110.0, bed_temp=99.0,
                      eta_last=1500.0, eta_last_at=445.0)),
                # Inside eta_window of the older anchor the last measured rate
                # stands: 2.0 C/min from 99.0 to 36 C is 31.5 min, and no
                # anchor moves.
                ("ramp-progress-held-rate", params, "", {}, {},
                 cool(last_degree=100, eta_bed=101.0, eta_at=300.0,
                      eta_bed_b=100.0, eta_at_b=400.0, meas_rate=2.0,
                      bed_temp=99.0)),
                ("ramp-heater", params, "", {}, {},
                 dict(cool(has_heater=1), **heater)),
                # The endpoint is reached by ELAPSED now: settle 180 plus the
                # full 1800 s schedule, so both axes snap to final together.
                # The schedule ending is no longer enough: the ramp completes
                # when the PLATE arrives. This is the arrival case.
                ("ramp-last-step", params, "", {}, {},
                 cool(elapsed=1980, bed_temp=35.0)),
                # And this is the case that used to finish early and abandon the
                # last few degrees -- schedule spent, plate still 5 C high. It
                # must HOLD, not finish.
                ("ramp-overrun", params, "", {}, {},
                 cool(elapsed=1980, bed_temp=40.0)),
                # finish_grace spent with the plate still out: stop, and say so
                # rather than "done".
                ("ramp-gave-up", params, "", {}, {},
                 cool(elapsed=1980, bed_temp=40.0, overrun_s=900.0)),
                # THE TICK SCALES WITH THE RATE (2026-10-13): ramp_tick / rate,
                # four ticks a degree. At 3 C/min from 90, 15 s in, the line is at
                # 89.25 and the target already 89: no step, and the next tick in
                # 5 s, not 15.
                ("ramp-tick-at-3c", params, "", {}, {},
                 cool(shipped_tick=True, b0=90.0, b_rate=3.0, c_rate=0.0, c_step=0.0,
                      elapsed=195, b_cmd=89.0, bed_temp=89.25)),
                # ...and 20 s in, the fourth tick, the line is exactly on 89 and
                # the target steps to 88 on that tick. A 15 s tick put this step
                # 0, 5 or 10 s late in turn, which is what the exhaust hunted on.
                ("ramp-tick-steps-on-time", params, "", {}, {},
                 cool(shipped_tick=True, b0=90.0, b_rate=3.0, c_rate=0.0, c_step=0.0,
                      elapsed=200, b_cmd=89.0, bed_temp=89.0)),
                # 15 s at 1 C/min, as before.
                ("ramp-tick-at-1c", params, "", {}, {},
                 cool(shipped_tick=True, b0=90.0, b_rate=1.0, c_rate=0.0, c_step=0.0,
                      elapsed=210, b_cmd=89.0, bed_temp=89.5)),
                # Clamped: 10 C/min would be 1.5 s, held at 2; 0.25 C/min would
                # be 60 s, held at 30.
                ("ramp-tick-clamped-fast", params, "", {}, {},
                 cool(shipped_tick=True, b0=90.0, b_rate=10.0, c_rate=0.0, c_step=0.0,
                      elapsed=183, b_cmd=89.0, bed_temp=89.5)),
                ("ramp-tick-clamped-slow", params, "", {}, {},
                 cool(shipped_tick=True, b0=90.0, b_rate=0.25, c_rate=0.0, c_step=0.0,
                      elapsed=300, b_cmd=89.0, bed_temp=89.5)),
                # THE LAST HALF DEGREE, AT FULL (2026-10-13). Schedule spent, the
                # plate 0.5 C above final with the assist engaged, at the shipped
                # settings and Z 23.7, where the published-default ramp on Zero2
                # ended. Both fans stay at their ceiling -- the PI had let them
                # back to 93 % and 87 % -- and 35.5 is not 35: no finish, which
                # with the old finish_tolerance of 1.0 it was.
                ("ramp-overrun-full", params, "", {}, {},
                 dict(cool(elapsed=1980, bed_temp=35.5, assist=1, assist_latched=1,
                           curtain_latched=1, assist_exhaust=0.93, assist_curtain=0.87,
                           assist_i=0.5, prev_err=0.6, overrun_s=60.0),
                      **{"toolhead": th_at(23.7),
                         "gcode_macro _COOLDOWN_ASSIST_CFG":
                         Mock(settings["gcode_macro _COOLDOWN_ASSIST_CFG"])})),
                ("abort-bed-override", params, "", {}, {},
                 dict(cool(), **{"heater_bed": Mock(temperature=108.0,
                                                    target=42.0)})),
                # The ramp no longer owns ch_target (2026-09-14), so a moved or
                # released ceiling is not its business: neither may abort it.
                ("chamber-target-ignored", params, "", {}, {},
                 cool(ch=48.0, ch_written=60.0)),
                ("chamber-release-ignored", params, "", {}, {},
                 cool(ch=0.0, ch_written=62.0, c_step=1.0)),
                ("watchdog", params, "", {}, {}, cool(elapsed=99999)),
                ("abort-new-print", params, "", {}, {},
                 dict(cool(), **{"print_stats":
                                 Mock(state="printing",
                                      info=Mock(current_layer=None))})),
                # ASSIST. The bed is below the ceiling and a long way behind the
                # schedule -- the regime where the heater has nothing left.
                ("assist-engage", params, "", {}, {},
                 cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=55.0)),
                # Just behind the aim: the exhaust must come up near its floor,
                # NOT at full. over 0.2 (the bed 0.05 C ahead of the line, 0.2
                # behind the aim a quarter-degree ahead of it): 0.13 + P 0.75 *
                # 0.2 + I on this first engaged tick 0.25 * 0.2 * 15 / 60 =
                # 0.0125, so 0.2925.
                ("assist-min-fan", params, "", {}, {},
                 cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=45.2)),
                # THE INTEGRAL ACCUMULATES while engaged and behind. Carried
                # I 0.2, over 0.5: P 0.375, I 0.2 + 0.25 * 0.5 / 4 = 0.23125,
                # demand 0.73625, all exhaust with the curtain disabled.
                ("assist-integral-accumulates", params, "", {}, {},
                 cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=45.5,
                      assist=1, assist_latched=1, assist_i=0.2, prev_err=0.25)),
                # AND BLEEDS OFF ahead of the aim. over -0.5, the bed half a
                # degree below its target with the heater holding it: P -0.375,
                # I 0.4 - 0.03125 = 0.36875, demand clamps to the 0.13 floor.
                # The fans must not keep pushing against the heater.
                ("assist-integral-bleeds", params, "", {}, {},
                 cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=44.5,
                      bed_power=0.1,
                      assist=1, assist_latched=1, assist_i=0.4, prev_err=-0.75)),
                # NO CLIMB WHILE THE HEATER FIRES (2026-10-13). Still 0.1 behind
                # the aim, but the heater at 5 % says the fans are cooling enough:
                # I stays at 0.2, so demand is 0.13 + P 0.075 + 0.2 = 0.405.
                ("assist-heater-holds-integral", params, "", {}, {},
                 cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=45.1,
                      bed_power=0.05,
                      assist=1, assist_latched=1, assist_i=0.2, prev_err=-0.15)),
                # THE RUN-AHEAD MAY ENGAGE ONLY AN IDLE HEATER. The line at
                # 45.375 (ramp time 1551 s), so the aim is 45.125; the target
                # last tick, with the line at 46.0, was 45. The bed at 45.1 is
                # above that target and 0.025 ahead of the aim, but closing at
                # 0.015 C/s from prev_err -0.5 it projects 0.425 behind. With
                # the heater at 0 that engages, at the exhaust floor.
                ("assist-lookahead-idle", params, "", {}, {},
                 cool(elapsed=1731, c_step=0.0, c_rate=0.0, bed_temp=45.1,
                      prev_err=-0.5)),
                # THE FIRST RAMP TICK STEPS AT ONCE (2026-10-13): ramp time 0,
                # the bed on its print target, and the target a degree below
                # it. The bed on the line reads 0.25 behind the aim, but it is
                # not above the target the heater last had, so nothing engages.
                ("ramp-first-tick", params, "", {}, {},
                 cool(elapsed=180, bed_temp=110.0)),
                # Same small error with the curtain ENABLED: demand 0.2925 is
                # below the exhaust's ceiling, so the exhaust carries it alone,
                # at the same duty as with the curtain disabled.
                ("assist-curtain-withheld", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=45.2),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                             Mock(**LINEAR_FANS, exhaust_enable=1, curtain_enable=1,
                                  lead=0.25, error_on=0.0, error_off=0.3,
                                  assist_kp=0.75, assist_ki=0.25,
                                  authority_power=0.02, exhaust_min=0.0,
                                  exhaust_max=1.0, curtain_span=5.0,
                                  curtain_max=1.0, bed_ceiling=60.0,
                                  curtain_strength_factor=1.0,
                                  curtain_latch=0, curtain_unlatch=0.05, curtain_hold=0,
                                  curtain_min=0.20, assist_latch=1,
                                  assist_lookahead=30.0,
                                  finish_tolerance=1.0, finish_grace=900.0)})),
                # Same error, bed ABOVE a bed_ceiling of 60. The gate is off by
                # default since 2026-10-13; set, it must still keep the curtain
                # off a plate above it.
                ("assist-ceiling", params, "", {}, {},
                 dict(cool(b_at=95.0, c_step=0.0, c_rate=0.0, bed_temp=108.0),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                             Mock(**LINEAR_FANS, exhaust_enable=1, curtain_enable=1,
                                  lead=0.25, error_on=0.0, error_off=0.3,
                                  assist_kp=0.75, assist_ki=0.25,
                                  authority_power=0.02, exhaust_min=0.0,
                                  exhaust_max=1.0, curtain_span=5.0,
                                  curtain_max=1.0, bed_ceiling=60.0,
                                  curtain_strength_factor=1.0,
                                  curtain_latch=0, curtain_unlatch=0.05, curtain_hold=0,
                                  curtain_min=0.20, assist_latch=1,
                                  assist_lookahead=30.0,
                                  finish_tolerance=1.0, finish_grace=900.0)})),
                # Latched on, error now inside the release band. Must release
                # rather than hold, or the fans never stop.
                ("assist-release", params, "", {}, {},
                 cool(b_at=54.0, c_step=0.0, c_rate=0.0, assist=1,
                      assist_curtain=0.3, bed_temp=52.0)),
                # Curtain explicitly enabled: the fan write must appear.
                ("assist-curtain", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=55.0),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                             Mock(**LINEAR_FANS, exhaust_enable=1, curtain_enable=1,
                                  lead=0.25, error_on=0.0, error_off=0.3,
                                  assist_kp=0.75, assist_ki=0.25,
                                  authority_power=0.02, exhaust_min=0.0,
                                  exhaust_max=1.0, curtain_span=5.0,
                                  curtain_max=1.0, bed_ceiling=60.0,
                                  curtain_strength_factor=1.0,
                                  curtain_latch=0, curtain_unlatch=0.05, curtain_hold=0,
                                  curtain_min=0.20, assist_latch=1,
                                  assist_lookahead=30.0,
                                  finish_tolerance=1.0, finish_grace=900.0)})),
                # NOR A HEATER STILL WORKING. 1.05 C behind the aim and above its
                # last target, but with the heater still at 17 % it is the
                # setpoint moving, not authority lost -- the first ramp tick of
                # the 2026-09-14 PI cooldown engaged on exactly this.
                ("assist-step-held", params, "", {}, {},
                 cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=46.05,
                      bed_power=0.17, prev_err=1.05)),
                # THE STEP SAWTOOTH MUST NOT REACH THE CONTROLLER. Ramp time
                # 1536 s puts the line at exactly 46.0, so the target steps from
                # 46 to 45 on this tick. The bed at 45.7 is 0.7 C above the new
                # target, but 0.3 ahead of the line and 0.05 ahead of the aim,
                # and below the 46 the heater last had: the assist stays off.
                # prev_err matches, so the run-ahead sees no slope either.
                ("assist-ignores-step-sawtooth", params, "", {}, {},
                 cool(c_step=0.0, c_rate=0.0, elapsed=1716, bed_temp=45.7,
                      prev_err=-0.3)),
                # THE SHIPPED SETTINGS HOLD A LATCHED CURTAIN. Demand 0.252
                # (over 0.15) is far below the latch at the exhaust's ceiling,
                # 1.0 on these identity curves. Reads cooldown.cfg's own _COOLDOWN_ASSIST_CFG, so
                # this tests the shipped values, not a fixture copy.
                ("curtain-holds-in-band", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=45.15,
                           assist=1, assist_latched=1, curtain_latched=1,
                           prev_err=-0.1),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                         Mock(dict(settings["gcode_macro _COOLDOWN_ASSIST_CFG"], **LINEAR_FANS,
                                   curtain_enable=1))})),
                # ONCE ON, IT STAYS ON (2026-10-12). Latched, and the plate back
                # on the schedule so demand sits on the floor: the shipped
                # curtain_hold keeps the curtain at its floor...
                ("curtain-held-on-schedule", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=45.0,
                           assist=1, assist_latched=1, curtain_latched=1),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                         Mock(dict(settings["gcode_macro _COOLDOWN_ASSIST_CFG"], **LINEAR_FANS,
                                   curtain_enable=1))})),
                # ...and curtain_hold: 0 brings the release band back.
                ("curtain-released-without-hold", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=45.0,
                           assist=1, assist_latched=1, curtain_latched=1),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                         Mock(dict(settings["gcode_macro _COOLDOWN_ASSIST_CFG"], **LINEAR_FANS,
                                   curtain_enable=1, curtain_hold=0))})),
                # ...BUT NOT A HEATER STILL WORKING. The same projection with the
                # bed at 20 % power must not engage: that is the 2026-09-13
                # early latch at the settle-to-ramp handover, with the plate at
                # 124 C and the heater drawing 30-60 W.
                ("assist-lookahead-held", params, "", {}, {},
                 dict(cool(elapsed=1731, c_step=0.0, c_rate=0.0, bed_temp=45.1,
                           prev_err=-0.5, bed_power=0.2),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                             Mock(**LINEAR_FANS, exhaust_enable=1, curtain_enable=1,
                                  lead=0.25, error_on=0.0, error_off=0.3,
                                  assist_kp=0.75, assist_ki=0.25,
                                  authority_power=0.02, exhaust_min=0.0,
                                  exhaust_max=1.0, curtain_span=5.0,
                                  curtain_max=1.0, bed_ceiling=60.0,
                                  curtain_strength_factor=1.0,
                                  curtain_latch=0, curtain_unlatch=0.05, curtain_hold=0,
                                  curtain_min=0.20, assist_latch=1,
                                  assist_lookahead=30.0,
                                  finish_tolerance=1.0, finish_grace=900.0)})),
                # THE HANDOVER IS A SWAP (2026-10-13). over 1.2: P 0.9 + I 0.075
                # on 0.13 is demand 1.105, past the exhaust's ceiling of 1.0, so
                # the curtain latches in at its floor, 0.2, and the exhaust gives
                # back exactly that: 0.905. Total cooling 1.105 either way.
                ("assist-latch-handover", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=46.2),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                             Mock(**LINEAR_FANS, exhaust_enable=1, curtain_enable=1,
                                  lead=0.25, error_on=0.0, error_off=0.3,
                                  assist_kp=0.75, assist_ki=0.25,
                                  authority_power=0.02, exhaust_min=0.0,
                                  exhaust_max=1.0, curtain_span=5.0,
                                  curtain_max=1.0, bed_ceiling=60.0,
                                  curtain_strength_factor=1.0,
                                  curtain_latch=0, curtain_unlatch=0.05, curtain_hold=0,
                                  curtain_min=0.20, assist_latch=1,
                                  assist_lookahead=30.0,
                                  finish_tolerance=1.0, finish_grace=900.0)})),
                # EXHAUST FIRST ABOVE IT. over 2.0: P 1.5 + I 0.125 on 0.13 is
                # demand 1.755. The exhaust is back at its ceiling, 1.0, and the
                # curtain carries the rest, 0.755.
                ("assist-exhaust-first-climb", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=47.0),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                             Mock(**LINEAR_FANS, exhaust_enable=1, curtain_enable=1,
                                  lead=0.25, error_on=0.0, error_off=0.3,
                                  assist_kp=0.75, assist_ki=0.25,
                                  authority_power=0.02, exhaust_min=0.0,
                                  exhaust_max=1.0, curtain_span=5.0,
                                  curtain_max=1.0, bed_ceiling=60.0,
                                  curtain_strength_factor=1.0,
                                  curtain_latch=0, curtain_unlatch=0.05, curtain_hold=0,
                                  curtain_min=0.20, assist_latch=1,
                                  assist_lookahead=30.0,
                                  finish_tolerance=1.0, finish_grace=900.0)})),
                # IN RPM, WITH k MEASURED AT THE PLATE'S HEIGHT (2026-10-13). The
                # shipped curves and fit, the curtain enabled, a 0.13 exhaust
                # floor. The numbers come from an independent model of the same
                # arithmetic, not from this render. At Z 22 (a 12 mm part plus
                # END_PRINT's 10) k is 3.06 and the exhaust's ceiling 1.041, so
                # 0.6 C behind -- demand 0.795 -- is the exhaust's alone: 0.702.
                ("assist-rpm-low-z-exhaust-alone", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=45.6),
                      **{"toolhead": th_at(22.0),
                         "gcode_macro _COOLDOWN_ASSIST_CFG":
                         Mock(settings["gcode_macro _COOLDOWN_ASSIST_CFG"])})),
                # 2 C behind with the plate at Z 160 (fitted at 155), where k is
                # 1.49: demand 1.933, the exhaust at its ceiling, 1.0, and the
                # curtain the rest at the weaker k -- 0.452.
                ("assist-rpm-high-z-curtain", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=47.0),
                      **{"toolhead": th_at(160.0),
                         "gcode_macro _COOLDOWN_ASSIST_CFG":
                         Mock(settings["gcode_macro _COOLDOWN_ASSIST_CFG"])})),
                # The same 2 C at Z 22, k 3.06: the exhaust at its ceiling and
                # the curtain needs only 0.21 for the rest.
                ("assist-rpm-low-z-exhaust-full", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=47.0),
                      **{"toolhead": th_at(22.0),
                         "gcode_macro _COOLDOWN_ASSIST_CFG":
                         Mock(settings["gcode_macro _COOLDOWN_ASSIST_CFG"])})),
                # THE SWAP AT k 3. 1.2 C behind at Z 22, demand 1.283: the
                # curtain's floor is worth 0.85 of exhaust rpm here, so the
                # exhaust gives most of its range back -- 0.264, curtain 0.2.
                ("assist-rpm-low-z-swap", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=46.2),
                      **{"toolhead": th_at(22.0),
                         "gcode_macro _COOLDOWN_ASSIST_CFG":
                         Mock(settings["gcode_macro _COOLDOWN_ASSIST_CFG"])})),
                # NO TEMPERATURE GATE (2026-10-13). The same 2 C behind with the
                # bed at 80 C: the curtain latches all the same. Until today
                # bed_ceiling 60 kept it off.
                ("assist-rpm-no-temperature-gate", params, "", {}, {},
                 dict(cool(b_at=78.0, c_step=0.0, c_rate=0.0, bed_temp=80.0),
                      **{"toolhead": th_at(22.0),
                         "gcode_macro _COOLDOWN_ASSIST_CFG":
                         Mock(settings["gcode_macro _COOLDOWN_ASSIST_CFG"])})),
                # ANTI-WINDUP. over 15, already engaged with I 1.5: 0.13 + P 11.25
                # + 1.5 is far past top 2.0, so both fans run flat out AND the
                # integral must not grow -- it is left exactly where it was, so
                # there is no write. Without this, a long saturated stretch would
                # bank enough integral to hold the fans on well after the plate
                # caught up.
                ("assist-windup-held", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=60.0,
                           assist=1, assist_latched=1, curtain_latched=1,
                           assist_i=1.5, prev_err=15.0),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                             Mock(**LINEAR_FANS, exhaust_enable=1, curtain_enable=1,
                                  lead=0.25, error_on=0.0, error_off=0.3,
                                  assist_kp=0.75, assist_ki=0.25,
                                  authority_power=0.02, exhaust_min=0.0,
                                  exhaust_max=1.0, curtain_span=5.0,
                                  curtain_max=1.0, bed_ceiling=60.0,
                                  curtain_strength_factor=1.0,
                                  curtain_latch=0, curtain_unlatch=0.05, curtain_hold=0,
                                  curtain_min=0.20, assist_latch=1,
                                  assist_lookahead=30.0,
                                  finish_tolerance=1.0, finish_grace=900.0)})),
                # THE RAMP DRIVES THE EXHAUST DIRECTLY (2026-09-14): a plain
                # SET_FAN_SPEED on _CH_HW.fan, with no chamber ceiling claimed
                # and no chamber loop started.
                ("assist-drives-exhaust", params, "", {}, {},
                 cool(b_at=45.0, c_cmd=0.0, c_step=0.0, c_rate=0.0, ch=0.0,
                      ch_written=0.0, bed_temp=55.0)),
                # Released while still holding a duty: the exhaust goes to zero
                # by the same direct write, and no chamber loop is stopped.
                ("assist-release-zeroes-exhaust", params, "", {}, {},
                 cool(b_at=54.0, c_cmd=0.0, c_step=0.0, c_rate=0.0, assist=1,
                      assist_exhaust=0.5, ch=32.0, ch_written=32.0, bed_temp=52.0)),
                # No chamber sensor at all. The assist does not need one.
                ("assist-no-chamber-sensor", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=55.0),
                      **{"temperature_sensor chamber_temp": None})),
            ]
        elif s == "gcode_macro _CHAMBER_GUARD":
            # The whole body is behind `running == 1`, so a scenario that does
            # not set it renders an empty macro and proves nothing. This is the
            # pair that matters: a completed job must stop the loop, UNLESS a
            # cooldown ramp still needs the exhaust.
            done_job = {"print_stats": Mock(state="complete",
                                            info=Mock(current_layer=None))}
            guard_state = {"running": 1, "adapt_print": 1, "fan_latched": 0}
            scenarios = [
                ("job-complete-no-cooldown", params, "", guard_state, {},
                 dict(done_job, **cool(active=0))),
                ("job-complete-cooling", params, "", guard_state, {},
                 dict(done_job, **cool())),
                ("cooldown-absent", params, "", guard_state, {},
                 dict(done_job, **{"gcode_macro _COOL_STATE": None,
                                   "gcode_macro _COOLDOWN_CFG": None})),
            ]
        elif s in ("gcode_macro END_PRINT", "gcode_macro CANCEL_PRINT",
                   "gcode_macro _CHAMBER_AUTOSTART"):
            # These three reach cooldown.cfg through `in printer`, so both
            # sides of that guard have to render.
            scenarios = [
                ("cooldown-installed", params, "S40", {}, {}, {}),
                ("cooldown-running", params, "S40", {}, {}, cool()),
                ("cooldown-absent", params, "S40", {}, {},
                 {"gcode_macro _COOL_STATE": None,
                  "gcode_macro _COOLDOWN_CFG": None}),
            ]
            if s == "gcode_macro _CHAMBER_AUTOSTART":
                # The three ways a print can arrive here, which differ only in
                # _CH_STATE and previously all ended in the same _CHAMBER_ON.
                scenarios += [
                    ("declined", params, "S40",
                     {"requested": 1, "control_declined": 1}, {}, {}),
                    ("slicer-target", params, "S40",
                     {"requested": 1, "control_declined": 0}, {}, {}),
                    ("inferred", params, "S40",
                     {"requested": 0, "control_declined": 0}, {}, {}),
                ]
            if s == "gcode_macro CANCEL_PRINT":
                # A job cancelled before START_PRINT must not leave its
                # handshake behind for the next one.
                scenarios += [
                    ("handshake-pending", params, "S40", {}, {},
                     {"gcode_macro _SVZERO_PACK": Mock(version="1.0.4", seen=1, mode="precision")}),
                    ("pack-absent", params, "S40", {}, {},
                     {"gcode_macro _SVZERO_PACK": None}),
                ]

        # The over-budget branches are forced with a deliberately tiny budget
        # rather than with a big request. Trimming the strip from 125 pixels to
        # 33 made both of them unreachable at the real budget, and the expect
        # assertions caught it -- the scenarios still rendered, they had simply
        # stopped testing the branch they were written for. Pinning the budget
        # keeps them honest at any chain length.
        starved = {"gcode_macro _LIGHT_CAL": Mock(
            ma_red=7.70, ma_green=7.76, ma_blue=7.76, ma_white=16.10,
            ma_quiescent=8.8, ma_budget=20.0, masked=[20])}
        # A lit strip to split away from. The masked pixels are dark in both,
        # because the wrapper put them there; the quad is LIGHT_PATTERN's
        # unhomed look, the case that forces the pixel-by-pixel midpoint.
        def strip(fn):
            return {"neopixel nozzle_light": Mock(color_data=[
                [0.0, 0.0, 0.0, 0.0] if i in (1, 20, 21) else fn(i)
                for i in range(1, 34)])}
        lit_white = strip(lambda i: [0.0, 0.0, 0.0, 1.0])
        quad = strip(lambda i: [0.15 if (i - 1) % 4 == c else 0.0 for c in range(4)])
        if s == "gcode_macro SET_LED":
            def observer_state(state='complete',homed='',target=40,active=False):
                return {'print_stats':Mock(state=state),'toolhead':Mock(homed_axes=homed),
                        'heater_bed':Mock(temperature=65,target=target),
                        'camera_tune':Mock(active=active)}
            scenarios = [
                ('observer-idle',None,'LED=nozzle_light WHITE=0.2 OBSERVER=1',{}, {},observer_state()),
                ('observer-printing',None,'LED=nozzle_light WHITE=0.2 OBSERVER=1',{}, {},observer_state(state='printing')),
                ('observer-paused',None,'LED=nozzle_light WHITE=0.2 OBSERVER=1',{}, {},observer_state(state='paused')),
                ('observer-unknown',None,'LED=nozzle_light WHITE=0.2 OBSERVER=1',{}, {},observer_state(state='unknown')),
                ('observer-homed',None,'LED=nozzle_light WHITE=0.2 OBSERVER=1',{}, {},observer_state(homed='xyz')),
                ('observer-preheat',None,'LED=nozzle_light WHITE=0.2 OBSERVER=1',{}, {},observer_state(target=100)),
                ('observer-tuning',None,'LED=nozzle_light WHITE=0.2 OBSERVER=1',{}, {},observer_state(active=True)),
                ("other-led", None, "LED=Screen_Colour RED=1", {}, {}, {}),
                ("chain", None, "LED=nozzle_light WHITE=0.2", {}, {}, {}),
                ("split-down", None, "LED=nozzle_light WHITE=0", {}, {}, lit_white),
                ("split-mixed", None, "LED=nozzle_light WHITE=0.5", {}, {}, quad),
                ("no-transmit", None, "LED=nozzle_light WHITE=0.2 TRANSMIT=0",
                 {}, {}, {}),
                ("index-masked", None, "LED=nozzle_light WHITE=1 INDEX=20",
                 {}, {}, {}),
                ("index-clear", None, "LED=nozzle_light WHITE=1 INDEX=7",
                 {}, {}, {}),
                ("chain-over", None, "LED=nozzle_light WHITE=1", {}, {}, starved),
                ("index-over", None, "LED=nozzle_light WHITE=1 INDEX=7",
                 {}, {}, starved),
            ]
        elif s == "gcode_macro _LIGHT_APPLY_STATE":
            def light_state(homed="xyz", last="unhomed", active=False, result=None):
                return {"toolhead": Mock(homed_axes=homed),
                        "gcode_macro _LIGHT_STATE": Mock(last=last),
                        "camera_tune": (Mock(active=active, last_result=Mock(result))
                                        if result is not None else None)}
            scenarios = [
                ("homed-default", params, "", {}, {}, light_state()),
                ("homed-selected", params, "", {}, {}, light_state(result=dict(status="selected",white=0.6))),
                ("tuning", params, "", {}, {}, light_state(active=True,result=dict(status="not-run"))),
                ("failed-retains", params, "", {}, {}, light_state(result=dict(status="interrupted-current-settings-retained"))),
                ("not-run", params, "", {}, {}, light_state(result=dict(status="not-run"))),
                ("unhomed", params, "", {}, {}, light_state(homed="",last="homed",result=dict(status="selected",white=0.6))),
                ("unchanged", params, "", {}, {}, light_state(last="homed",result=dict(status="selected",white=0.6))),
                # A running print is lit before anything homes (2026-10-13), so
                # the watcher cannot put the unhomed pattern back ahead of M191.
                ("printing-unhomed", params, "", {}, {},
                 dict(light_state(homed="", last="unhomed", result=dict(status="not-run")),
                      print_stats=Mock(state="printing"))),
            ]
        elif s == "gcode_macro LIGHT_PATTERN":
            scenarios = [
                ("counting", Mock(COUNT="20"), "COUNT=20", {}, {}, {}),
                ("over-budget", Mock(COUNT="20", VALUE="1.0"),
                 "COUNT=20 VALUE=1.0", {}, {}, starved),
            ]
        elif s == "gcode_macro NOZZLE_LIGHT":
            scenarios = [
                ("whole-chain", Mock(W="0.05"), "W=0.05", {}, {}, {}),
                ("spotlight", Mock(W="1.0", INDEX="7"), "W=1.0 INDEX=7", {}, {}, {}),
                # The solver, at the real budget and the real mask: 33 pixels
                # less [1, 20, 21] is 30 live, (1000 - 8.8) / 30 = 33.04 mA
                # each, white takes 16.10 of it and 16.94 is left for three
                # colour dies costing 23.22 together.
                # MAX=1, not a bare MAX: NOZZLE_LIGHT takes extended parsing
                # and the dispatcher rejects a bare word with "Malformed
                # command" before the macro runs. Passing params in directly
                # here hid that on 2026-09-03, so both halves are spelled out.
                ("max", Mock(MAX="1"), "MAX=1", {}, {}, {}),
                # Same solve where white alone cannot reach full: 20 mA budget
                # over 32 live pixels is 0.35 mA each, so white truncates to
                # 0.0217 and there is nothing left to buy colour with.
                ("max-starved", Mock(MAX="1"), "MAX=1", {}, {}, starved),
            ]
        elif s == "gcode_macro SET_VELOCITY_LIMIT":
            # Precision mode's ceiling clamp. Params come from the raw text, as
            # Klipper builds them, so the clamp sees the strings it will see.
            scenarios = [
                ("under", None, "VELOCITY=200 ACCEL=3000", {}, {}),
                ("over", None, "VELOCITY=500 ACCEL=10000 SQUARE_CORNER_VELOCITY=3", {}, {}),
                ("scv-only", None, "SQUARE_CORNER_VELOCITY=5", {}, {}),
            ]
        elif s == "gcode_macro M204":
            # Traditional G-code: S10000 reaches the macro as {"S": "10000"}.
            scenarios = [
                ("under", None, "S3000", {}, {}),
                ("over", None, "S10000", {}, {}),
                ("over-pt", None, "P6000 T8000", {}, {}),
            ]

        # An optional 7th element: a string the rendered output MUST contain.
        # Jinja only evaluates the branch it takes, so a scenario that quietly
        # lands in the wrong one renders fine and validates nothing -- the
        # branch it was written for is still unrendered. Naming the expected
        # output turns "it rendered" into "it rendered THIS".
        expect = {
            ("gcode_macro M191", "r-heating"): ["CHAMBER_PREHEAT_WAIT MINIMUM=45.0",
                                                "the slicer's target"],
            ("gcode_macro M191", "r-cooling"): ["M141 S35.0",
                                                "CHAMBER_COOL_WAIT MAXIMUM=35.0 FAN=exhaust_fan"],
            ("gcode_macro M191", "s-beats-r"): "CHAMBER_PREHEAT_WAIT MINIMUM=45.0",
            ("gcode_macro M191", "tol-ignored"): ["TOL is not an M191 argument",
                                                  "CHAMBER_PREHEAT_WAIT MINIMUM=45.0"],
            ("gcode_macro M191", "wipe-lead-learned"):
                "nozzle wipe starts 48 s before the chamber is due (configured 70 s, shortest wipe so far 60 s)",
            ("gcode_macro M191", "wipe-lead-configured"):
                "nozzle wipe starts 70 s before the chamber is due",
            ("gcode_macro M191", "exact-minimum"): ["the chamber to reach 30.0C, the slicer's minimum",
                                                    "BOOST=1"],
            ("gcode_macro M191", "boost-off"): "BOOST=0",
            ("gcode_macro M191", "default-compatibility"): "the floor for a bare M191",
            ("gcode_macro _CH_ADAPT_BEGIN", "published-defaults"):
                "Adaptive floor 0.15, lowering cooldown 0",
            ("gcode_macro _CH_ADAPT_BEGIN", "opt-in-trial"):
                "Adaptive floor trial 0.135, known-good 0.14",
            ("gcode_macro SVZERO_MODE", "report"): "machine mode standard, automatic switching on",
            ("gcode_macro SVZERO_MODE", "already"): "already in standard mode",
            ("gcode_macro SVZERO_MODE", "off"): "automatic switching is off",
            ("gcode_macro SVZERO_MODE", "ask"): "asked svzero_mode_switch for precision mode",
            ("gcode_macro SVZERO_REQUIRE", "mode-match-switch"): "VARIABLE=seen VALUE=1",
            ("gcode_macro SVZERO_REQUIRE", "precision-applied"): "VARIABLE=seen VALUE=1",
            ("gcode_macro SET_VELOCITY_LIMIT", "under"): "_SVZERO_SVL_BASE VELOCITY=200 ACCEL=3000",
            ("gcode_macro SET_VELOCITY_LIMIT", "over"): [
                "_SVZERO_SVL_BASE VELOCITY=300.0 ACCEL=4000.0 SQUARE_CORNER_VELOCITY=3",
                "precision mode clamped VELOCITY=500>300.0 ACCEL=10000>4000.0"],
            ("gcode_macro SET_VELOCITY_LIMIT", "scv-only"): "_SVZERO_SVL_BASE SQUARE_CORNER_VELOCITY=5",
            ("gcode_macro M204", "under"): "M99204 S3000",
            ("gcode_macro M204", "over"): ["M99204 S4000.0", "precision mode clamped M204 S=10000>4000.0"],
            ("gcode_macro M204", "over-pt"): "M99204 P4000.0 T4000.0",
            ("gcode_macro _COOLDOWN_START", "heaterless"): "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER",
            # The cooldown no longer touches the chamber heater at start; the
            # paired forbid proves it, this proves the ramp still armed.
            ("gcode_macro _COOLDOWN_START", "heater"): "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER",
            ("gcode_macro _COOLDOWN_START", "chamber-rate-ignored"):
                ["chamber rate 1.0C/min ignored", "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER"],
            ("gcode_macro _COOLDOWN_TAKE_OVER", ""): "_CHAMBER_OFF",
            ("gcode_macro _COOLDOWN_START", "bed-only"): "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER",
            ("gcode_macro _COOLDOWN_START", "nothing-to-do"): "nothing to do, the bed is already at or below 35C",
            # One bed rate for every material (2026-10-12): 110 C to 35 C at
            # the published 2 C/min (2026-10-13) is 37.5 min, shown as 38, and
            # no material was named.
            ("gcode_macro _COOLDOWN_START", "bed-only"):
                ["bed 110C to 35C at 2.0C/min, 38 min on schedule (no material named)",
                 "holding the bed at 110C until the nozzle cools to it, at most 10 min",
                 "VARIABLE=last_degree VALUE=110"],
            ("gcode_macro _COOLDOWN_TICK", "settle-hold"): "DURATION=5",
            # The ramp begins by switching every chamber controller off.
            ("gcode_macro _COOLDOWN_TICK", "settle-nozzle-cooled"):
                ["the nozzle has cooled to the bed (95C) after 1 min, ramp starting",
                 "_COOLDOWN_TAKE_OVER", "VARIABLE=last_degree VALUE=118",
                 "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER DURATION=0.1",
                 "VARIABLE=eta_bed_b VALUE=118.0"],
            ("gcode_macro _COOLDOWN_TICK", "settle-cap"):
                ["the nozzle is still at 250C, above the bed, after 10 min. Ramp starting anyway",
                 "_COOLDOWN_TAKE_OVER"],
            ("gcode_macro START_PRINT", "load-cell"): "METHOD=force_overlay",
            ("gcode_macro START_PRINT", "camera-tuner"): "CAMERA_TUNE STARTUP=1",
            # Arcs, not straight-line approximations, and a full circle has no
            # endpoint so gcode_arcs takes its 2*pi branch.
            ("gcode_macro CHAMBER_ORBIT_SWEEP", "default"): "_CHAMBER_ORBIT_CYCLE",
            # M400 before each announcement, or the console races the motion.
            ("gcode_macro ORBIT_WHINE_SWEEP", "default"): "M400",
            ("gcode_macro ORBIT_WHINE_SWEEP", "over-accel"): "over max_accel",
            # Every arc in ONE script: that is what makes the lookahead blend
            # them instead of stopping at each junction.
            ("gcode_macro _CHAMBER_ORBIT_CYCLE", "full"): "G2 X42.0 Y100.0 I-58.0 J0 F141",
            # Pins the whole construction: 6 outbound arcs, the rim semicircle,
            # 6 interleaved inbound arcs, the hub semicircle.
            # 18 arcs now that the rim reaches the centreline edge, and the
            # feed climbs with radius so dwell tracks the bed's excess heat.
            ("gcode_macro _CHAMBER_ORBIT_BEGIN", ""): "arcs (out and back)",
            ("gcode_macro _CHAMBER_ORBIT_STEP", "mid-sweep"): "G2 X126.0 Y100.0 I22.0 J0 F104",
            # Index past the end wraps straight onto the first arc, which is
            # where the last inbound arc already left the toolhead.
            ("gcode_macro _CHAMBER_ORBIT_STEP", "wraps"): "G2 X82.0 Y100.0 I-14.0 J0 F101",
            # Out and back in one armed cycle.
            # 26 rings from 10 to 60 mm at a 2 mm step, the largest sweep that
            # fits 60 s at 6000 mm/min.
            ("gcode_macro SET_CLEAN_TEMP", "exact"): "M104 S195",
            ("gcode_macro _BRUSH_INFER_CLEAN_TEMP", "from-spool"): "SET_CLEAN_TEMP MATERIAL=ABS",
            ("gcode_macro _BRUSH_INFER_CLEAN_TEMP", "from-last-print"): "SET_CLEAN_TEMP TEMP=190",
            ("gcode_macro _BRUSH_INFER_CLEAN_TEMP", "blind"): "no print history",
            ("gcode_macro SET_CLEAN_TEMP", "prefix"): "M104 S170",
            ("gcode_macro SET_CLEAN_TEMP", "fallback"): "M104 S200",
            ("gcode_macro SET_CLEAN_TEMP", "explicit"): "M104 S150",
            ("gcode_macro _PREFLIGHT", "hook"): "_SVZERO_PRINT_START_HOOK",
            ("gcode_macro SET_CLEAN_TEMP", "bed-coming-down"):
                ["M104 S150", "silicone wipe at 195C (abs), nozzle held at 150C until the bed is at its target",
                 "VARIABLE=clean_temp VALUE=195.0"],
            # A VERSION mismatch warns and never refuses, in both directions.
            # A MODE mismatch refuses -- see `refuse` below.
            ("gcode_macro SVZERO_REQUIRE", "version-mismatch"): "profile expects pack 0.9.0",
            ("gcode_macro SVZERO_REQUIRE", "version-mismatch-mode-match"): "profile expects pack 0.9.0",
            ("gcode_macro SVZERO_REQUIRE", "mode-match"): "VARIABLE=seen VALUE=1",
            ("gcode_macro SVZERO_REQUIRE", "mode-match-precision"): "VARIABLE=seen VALUE=1",
            ("gcode_macro SVZERO_REQUIRE", "mode-match-case"): "VARIABLE=seen VALUE=1",
            ("gcode_macro _SVZERO_CHECK_PROFILE", "no-handshake"): "not sliced by an rpcyan SV Zero profile",
            ("gcode_macro _SVZERO_CHECK_PROFILE", "no-handshake-standard"): "not sliced by an rpcyan SV Zero profile",
            ("gcode_macro CANCEL_PRINT", "handshake-pending"): "MACRO=_SVZERO_PACK VARIABLE=seen VALUE=0",
            ("gcode_macro _COOLDOWN_TICK", "settle-progress"):
                "holding the bed at 118C, nozzle 180C. The ramp starts when the nozzle reaches the bed, 5 min at most.",
            # The bed setpoint is commanded in whole degrees: at 460 -> 475 s of
            # a 2.5 C/min bed leg from 110 the line sits at 90.21, which must be
            # commanded as 90 and not as 90.21.
            ("gcode_macro _COOLDOWN_TICK", "ramp-integer"): "M140 S90",
            ("gcode_macro _COOLDOWN_TICK", "ramp-progress"):
                ["bed 100C, about 37 min to 35C, heater 0%, exhaust 100%",
                 "bed 99C, about 37 min to 35C, heater 0%, exhaust 100%",
                 "VARIABLE=last_degree VALUE=99", "VARIABLE=meas_rate VALUE=1.435",
                 "VARIABLE=eta_last VALUE=2240.0", "VARIABLE=eta_at_b VALUE=460",
                 "VARIABLE=heat_n VALUE=0",
                 "the bed (99C) is cooling slower than the schedule (90C), the fans are helping"],
            ("gcode_macro _COOLDOWN_TICK", "ramp-same-degree"): "VARIABLE=elapsed",
            ("gcode_macro _COOLDOWN_TICK", "ramp-progress-smoothed"):
                ["bed 99C, about 29 min to 35C", "VARIABLE=eta_last VALUE=1711.5"],
            ("gcode_macro _COOLDOWN_TICK", "ramp-progress-held-rate"):
                ["bed 99C, about 32 min to 35C", "VARIABLE=last_degree VALUE=99"],
            # The plate arrived: finish.
            ("gcode_macro _COOLDOWN_TICK", "ramp-last-step"): "done, bed 35C after 33 min. Heaters off.",
            # The plate did not: hold the assist and say so, rather than
            # declaring victory 5 C short the way it did on 2026-09-07.
            ("gcode_macro _COOLDOWN_TICK", "ramp-overrun"):
                ["schedule finished with the bed at 40C, waiting up to 15 min for it to reach 35C",
                 "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER"],
            ("gcode_macro _COOLDOWN_TICK", "ramp-gave-up"):
                ["stopped with the bed at 40C, 15 min past the schedule. Heaters off.", "_COOLDOWN_FINISH"],
            ("gcode_macro _COOLDOWN_TICK", "ramp-tick-at-3c"):
                ["UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER DURATION=5.0", "VARIABLE=elapsed VALUE=200.0"],
            ("gcode_macro _COOLDOWN_TICK", "ramp-tick-steps-on-time"):
                ["M140 S88", "VARIABLE=b_cmd VALUE=88", "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER DURATION=5.0"],
            ("gcode_macro _COOLDOWN_TICK", "ramp-tick-at-1c"): "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER DURATION=15.0",
            ("gcode_macro _COOLDOWN_TICK", "ramp-tick-clamped-fast"): "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER DURATION=2.0",
            ("gcode_macro _COOLDOWN_TICK", "ramp-tick-clamped-slow"): "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER DURATION=30.0",
            ("gcode_macro _COOLDOWN_TICK", "ramp-overrun-full"):
                ["SET_FAN_SPEED FAN=exhaust_fan SPEED=1.0", "SET_FAN_SPEED FAN=fan2 SPEED=1.0"],
            ("gcode_macro _COOLDOWN_TICK", "ramp-heaterless"): "VARIABLE=elapsed",
            ("gcode_macro _COOLDOWN_TICK", "ramp-heater"): "VARIABLE=elapsed",
            ("gcode_macro _COOLDOWN_TICK", "abort-bed-override"): "the bed target changed to 42C outside the cooldown",
            ("gcode_macro _COOLDOWN_TICK", "chamber-target-ignored"): "VARIABLE=elapsed",
            ("gcode_macro _COOLDOWN_TICK", "chamber-release-ignored"): "VARIABLE=elapsed",
            ("gcode_macro _CHAMBER_VENT", "settle-leaves-vent"): "VARIABLE=vent_active VALUE=1",
            ("gcode_macro _COOLDOWN_TICK", "watchdog"): "stopped by the watchdog after 1667 min, heaters off",
            ("gcode_macro _COOLDOWN_TICK", "abort-new-print"): "_COOLDOWN_CLEAR KEEP_POLICY=1",
            ("gcode_macro _COOLDOWN_CLEAR", "clears-policy"): "VARIABLE=policy_set VALUE=0",
            ("gcode_macro _CHAMBER_VENT", "floor-set-runs"): "VARIABLE=vent_active VALUE=1",
            ("gcode_macro _COOLDOWN_TICK", "assist-engage"): "fan assist on at bed 55C, 9.8C behind the schedule, heater idle: exhaust 100%",
            # Above the ceiling the curtain is gated off, and the exhaust gets
            # its whole range: err 13 saturates it at 1.0.
            ("gcode_macro _COOLDOWN_TICK", "assist-ceiling"):
                ["fan assist on", "SET_FAN_SPEED FAN=exhaust_fan SPEED=1.0"],
            ("gcode_macro _COOLDOWN_TICK", "assist-release"): "fan assist off at bed 52C, back on schedule",
            # A large error puts the fans at their ceiling. err 10, P alone is
            # 6.75 on 0.13, far past top 2.0: both fans flat out, and no
            # integral banked on top of a saturation.
            ("gcode_macro _COOLDOWN_TICK", "assist-curtain"):
                ["SET_FAN_SPEED FAN=fan2 SPEED=1.0", "SET_FAN_SPEED FAN=exhaust_fan SPEED=1.0"],
            # The swap: the curtain on its floor and the exhaust down by exactly
            # what that adds (2026-10-13).
            ("gcode_macro _COOLDOWN_TICK", "assist-latch-handover"):
                ["SET_FAN_SPEED FAN=fan2 SPEED=0.2", "SET_FAN_SPEED FAN=exhaust_fan SPEED=0.905"],
            ("gcode_macro _COOLDOWN_TICK", "assist-rpm-low-z-exhaust-alone"): "SET_FAN_SPEED FAN=exhaust_fan SPEED=0.702",
            ("gcode_macro _COOLDOWN_TICK", "assist-rpm-high-z-curtain"):
                ["SET_FAN_SPEED FAN=fan2 SPEED=0.452", "SET_FAN_SPEED FAN=exhaust_fan SPEED=1.0",
                 "(k 1.49 at Z 160.0)"],
            ("gcode_macro _COOLDOWN_TICK", "assist-rpm-low-z-exhaust-full"):
                ["SET_FAN_SPEED FAN=fan2 SPEED=0.21", "SET_FAN_SPEED FAN=exhaust_fan SPEED=1.0",
                 "(k 3.06 at Z 22.0)"],
            ("gcode_macro _COOLDOWN_TICK", "assist-rpm-low-z-swap"):
                ["curtain fan on: exhaust 26%, curtain 20% (k 3.06 at Z 22.0)",
                 "SET_FAN_SPEED FAN=fan2 SPEED=0.2", "SET_FAN_SPEED FAN=exhaust_fan SPEED=0.264"],
            ("gcode_macro _COOLDOWN_TICK", "assist-rpm-no-temperature-gate"):
                ["SET_FAN_SPEED FAN=fan2 SPEED=0.21", "SET_FAN_SPEED FAN=exhaust_fan SPEED=1.0"],
            ("gcode_macro _COOLDOWN_TICK", "assist-exhaust-first-climb"):
                ["SET_FAN_SPEED FAN=fan2 SPEED=0.755", "SET_FAN_SPEED FAN=exhaust_fan SPEED=1.0"],
            # Saturated and held: flat out, and the integral left alone (see
            # the paired forbid).
            ("gcode_macro _COOLDOWN_TICK", "assist-windup-held"):
                ["SET_FAN_SPEED FAN=fan2 SPEED=1.0", "SET_FAN_SPEED FAN=exhaust_fan SPEED=1.0"],
            # Both halves of the integral, named: the duty it produced and the
            # value it banked. 0.293, not the 0.2925 of the arithmetic -- Jinja
            # 2.11's round(3) on the printer, read off DUMP rather than guessed.
            ("gcode_macro _COOLDOWN_TICK", "assist-min-fan"):
                ["SET_FAN_SPEED FAN=exhaust_fan SPEED=0.293", "VARIABLE=assist_i VALUE=0.0125"],
            ("gcode_macro _COOLDOWN_TICK", "assist-integral-accumulates"):
                ["SET_FAN_SPEED FAN=exhaust_fan SPEED=0.736", "VARIABLE=assist_i VALUE=0.2313"],
            ("gcode_macro _COOLDOWN_TICK", "assist-integral-bleeds"):
                ["SET_FAN_SPEED FAN=exhaust_fan SPEED=0.13", "VARIABLE=assist_i VALUE=0.3688"],
            ("gcode_macro _COOLDOWN_TICK", "assist-lookahead-idle"):
                ["fan assist on at bed 45C, 0.3C ahead of the schedule", "SET_FAN_SPEED FAN=exhaust_fan SPEED=0.13"],
            ("gcode_macro _COOLDOWN_TICK", "ramp-first-tick"): ["M140 S109", "VARIABLE=b_cmd VALUE=109"],
            ("gcode_macro _COOLDOWN_TICK", "assist-heater-holds-integral"): "SET_FAN_SPEED FAN=exhaust_fan SPEED=0.405",
            # Paired with its forbid: proves the ramp branch really rendered,
            # so "no assist engaged" means the heater gate held rather than the
            # scenario landing somewhere else entirely.
            ("gcode_macro _COOLDOWN_TICK", "assist-step-held"): "VARIABLE=elapsed",
            ("gcode_macro _COOLDOWN_TICK", "assist-ignores-step-sawtooth"): ["M140 S45", "VARIABLE=elapsed"],
            ("gcode_macro _COOLDOWN_TICK", "curtain-holds-in-band"): "SET_FAN_SPEED FAN=fan2 SPEED=0.2",
            ("gcode_macro _COOLDOWN_TICK", "curtain-held-on-schedule"):
                ["SET_FAN_SPEED FAN=fan2 SPEED=0.2", "SET_FAN_SPEED FAN=exhaust_fan SPEED=0.13"],
            ("gcode_macro _COOLDOWN_TICK", "curtain-released-without-hold"):
                ["curtain fan off: exhaust 13%", "VARIABLE=curtain_latched VALUE=0"],
            # 0.293, the SAME as with the curtain disabled. Allowing a second
            # fan must not weaken the first: the curtain changes who delivers a
            # demand, never how much is asked for. Under the 2026-09-08 law
            # this was squeezed to 0.15.
            ("gcode_macro _COOLDOWN_TICK", "assist-curtain-withheld"): "SET_FAN_SPEED FAN=exhaust_fan SPEED=0.293",
            # A bed-only ramp claims a NEUTRAL ceiling -- the chamber's own
            # 30 C -- so the PI asks for nothing and the assist floor alone
            # drives the fan. It used to claim chamber-6 = 24, which saturates.
            ("gcode_macro _COOLDOWN_TICK", "assist-drives-exhaust"): "SET_FAN_SPEED FAN=exhaust_fan SPEED=1.0",
            ("gcode_macro _COOLDOWN_TICK", "assist-no-chamber-sensor"): "SET_FAN_SPEED FAN=exhaust_fan SPEED=1.0",
            # Paired with the forbid below: this proves the scenario really did
            # schedule a ramp, so "no _CHAMBER_ON" means the guard worked rather
            # than that the macro took some other branch entirely.
            ("gcode_macro _COOLDOWN_TICK", "assist-release-zeroes-exhaust"):
                ["fan assist off", "SET_FAN_SPEED FAN=exhaust_fan SPEED=0.0"],
            ("gcode_macro END_PRINT", "cooldown-installed"): ["_COOLDOWN_START", "SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=5.0"],
            ("gcode_macro END_PRINT", "cooldown-absent"): ["TURN_OFF_HEATERS", "SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=5.0"],
            ("gcode_macro CANCEL_PRINT", "cooldown-installed"): ["COOLDOWN_ABORT", "SET_VELOCITY_LIMIT SQUARE_CORNER_VELOCITY=5.0"],
            ("gcode_macro _CHAMBER_GUARD", "job-complete-no-cooldown"): "_CHAMBER_OFF",
            ("gcode_macro _CHAMBER_GUARD", "job-complete-cooling"): "UPDATE_DELAYED_GCODE ID=CHAMBER_GUARD",
            ("gcode_macro _CHAMBER_GUARD", "cooldown-absent"): "_CHAMBER_OFF",
            ("gcode_macro _CHAMBER_AUTOSTART", "cooldown-installed"): "COOLDOWN_ABORT",
            # M141 S0 is a decision and must be recorded as one; any positive
            # target must clear that record rather than inherit it.
            ("gcode_macro M141", "ramp-owns-it"): "deferred",
            ("gcode_macro M141", "declined"):
                "VARIABLE=control_declined VALUE=1",
            ("gcode_macro M141", "standalone"):
                "VARIABLE=control_declined VALUE=0",
            ("gcode_macro _CHAMBER_AUTOSTART", "declined"):
                "chamber control off",
            ("gcode_macro _CHAMBER_AUTOSTART", "slicer-target"):
                "as requested by the slicer",
            ("gcode_macro _CHAMBER_AUTOSTART", "inferred"): "inferred",
            # The budget branches. A whole-chain 0.05 fits, a single pixel at
            # full fits and must carry its INDEX through, and the whole chain at
            # full white cannot fit and must come back scaled.
            # NOZZLE_LIGHT enforces nothing now: it hands off and then reports.
            ("gcode_macro NOZZLE_LIGHT", "whole-chain"): "LIGHT_STATUS",
            ("gcode_macro NOZZLE_LIGHT", "spotlight"): "INDEX=7",
            ("gcode_macro LIGHT_PATTERN", "counting"): "INDEX=20",
            ("gcode_macro _LIGHT_APPLY_STATE", "homed-default"): "NOZZLE_LIGHT W=0.5",
            ("gcode_macro _LIGHT_APPLY_STATE", "homed-selected"): "NOZZLE_LIGHT W=0.6",
            ("gcode_macro _LIGHT_APPLY_STATE", "not-run"): "NOZZLE_LIGHT W=0.5",
            ("gcode_macro _LIGHT_APPLY_STATE", "unhomed"): "LIGHT_PATTERN COUNT=33 VALUE=0.15",
            ("gcode_macro _LIGHT_APPLY_STATE", "printing-unhomed"): ["NOZZLE_LIGHT W=0.5", "VALUE='\"homed\"'"],
            ("gcode_macro _LIGHT_APPLY_STATE", "failed-retains"): "SET_GCODE_VARIABLE MACRO=_LIGHT_STATE",
            # Name the numbers, not just "it rendered". These are the whole
            # claim MAX makes, and an arithmetic slip that still renders is
            # exactly the failure this file exists to catch.
            ("gcode_macro NOZZLE_LIGHT", "max"):
                "RED=0.7295 GREEN=0.7295 BLUE=0.7295 WHITE=1.0",
            ("gcode_macro NOZZLE_LIGHT", "max-starved"):
                "RED=0.0 GREEN=0.0 BLUE=0.0 WHITE=0.0217",
            ("gcode_macro LIGHT_PATTERN", "over-budget"): "refused",
            # The mask wrapper. Another LED passes through untouched, a
            # whole-chain call is followed by a zeroing of the masked index,
            # a masked INDEX is zeroed, and an unmasked INDEX is not.
            ("gcode_macro SET_LED", "other-led"): "LED=Screen_Colour",
            ("gcode_macro SET_LED", "observer-idle"): "SET_LED_BASE LED=nozzle_light",
            # Every transmitting change goes out as two frames, halfway then
            # the target. Name both numbers: a midpoint that silently equals
            # the target renders fine and softens nothing.
            ("gcode_macro SET_LED", "chain"): ["INDEX=20", "WHITE=0.1 ", "WHITE=0.2 "],
            ("gcode_macro SET_LED", "index-masked"): "INDEX=20",
            ("gcode_macro SET_LED", "index-clear"):
                ["WHITE=0.5 INDEX=7 TRANSMIT=1", "WHITE=1.0 INDEX=7"],
            # Off is a step too: full white down to dark passes through 0.5.
            ("gcode_macro SET_LED", "split-down"): ["WHITE=0.5 ", "WHITE=0.0 "],
            # A mixed chain is split pixel by pixel. Pixel 2 is green 0.15 in
            # the quad, so its midpoint toward white 0.5 is green 0.075 and
            # white 0.25, and only the last pixel transmits.
            ("gcode_macro SET_LED", "split-mixed"):
                ["GREEN=0.075 BLUE=0.0 WHITE=0.25 INDEX=2 TRANSMIT=0",
                 "INDEX=33 TRANSMIT=1"],
            ("gcode_macro SET_LED", "no-transmit"): "WHITE=0.2 ",
            # D1 must descend ONTO the bristles before heating. pad_top 0.2 +
            # heat_air 0.0, formatted to 3 dp. Naming the number is the point:
            # until 2026-09-08 this heated at park_z (5.0), and that rendered
            # perfectly well while capturing no ooze at all.
            ("gcode_macro CLEAN_NOZZLE", ""): "G1 Z0.200",
            # Name the number: 900 - 800 = 100.0s, and the saved variable too.
            ("gcode_macro _BRUSH_MARK_DONE", "measured"):
                ["wipe took 100.0s", "SAVE_VARIABLE VARIABLE=wipe_seconds VALUE=100.0"],
            ("gcode_macro _BRUSH_MARK_DONE", "outside-print"): "not measurable",
            ("gcode_macro _BRUSH_MARK_DONE", "not-shortest"): "shortest still 60.0s",
            # Renormalization: scaled down, and never silent about it.
            ("gcode_macro SET_LED", "chain-over"): "renormalized",
            ("gcode_macro SET_LED", "index-over"): "renormalized",
        }
        # The inactive tick must produce nothing at all.
        forbid = {("gcode_macro SET_LED", "other-led"): "INDEX=20",
                  # The curtain's floor alone outweighs this demand at Z 22.
                  ("gcode_macro _COOLDOWN_TICK", "assist-rpm-low-z-exhaust-alone"): "FAN=fan2",
                  ("gcode_macro M191", "r-cooling"): ["G28", "CHAMBER_PREHEAT_WAIT"],
                  ("gcode_macro M191", "s-beats-r"): "CHAMBER_COOL_WAIT",
                  ("gcode_macro M191", "boost-off"): "bed boost on",
                  ("gcode_macro _CH_ADAPT_BEGIN", "published-defaults"): "VARIABLE=trial_active VALUE=1",
                  ("gcode_macro SET_VELOCITY_LIMIT", "under"): "clamped",
                  ("gcode_macro SET_VELOCITY_LIMIT", "scv-only"): "clamped",
                  ("gcode_macro M204", "under"): "clamped",
                  # A step that cannot be held must emit no arc at all.
                  ("gcode_macro ORBIT_WHINE_SWEEP", "over-accel"): "G2 ",
                  ("gcode_macro _CHAMBER_ORBIT_CYCLE", "inactive"): "G2 ",
                  ("gcode_macro _CHAMBER_ORBIT_STEP", "inactive"): "G3 ",
                  # The clamp must bite: axis_maximum is 200 in the fixture, so
                  # a ring at 500 mm would be off the machine entirely.
                  # No straight dash home: the inbound leg retraces the arcs.
                  ("gcode_macro _CHAMBER_ORBIT_STEP", "wraps"): "G1 ",
                  # On a machine with no load cell, forcing the overlay costs a
                  # heat-up and a contact probe on EVERY print.
                  ("gcode_macro START_PRINT", "eddy-only"): ["METHOD=force_overlay", "CAMERA_TUNE"],
                  ("gcode_macro START_PRINT", "load-cell"): "CAMERA_TUNE",
                  # A matching version must say nothing at all.
                  ("gcode_macro SVZERO_REQUIRE", "version-match"): "profile expects",
                  ("gcode_macro SVZERO_REQUIRE", "mode-match"): "profile expects",
                  ("gcode_macro _SVZERO_CHECK_PROFILE", "handshake-seen"): "not sliced by",
                  ("gcode_macro _SVZERO_CHECK_PROFILE", "handshake-seen-precision"): "not sliced by",
                  # Never abort on a version: the whole point is graceful
                  # degradation. (A raise fails any scenario not in `refuse`.)
                  ("gcode_macro SVZERO_REQUIRE", "version-mismatch"): "action_raise_error",
                  ("gcode_macro CANCEL_PRINT", "pack-absent"): "_SVZERO_PACK",
                  # Paired with the "M140 S90" expect: a trailing decimal point
                  # is the failure mode the integer snap exists to prevent, and
                  # a plain substring expect would happily match "M140 S90.21".
                  ("gcode_macro _COOLDOWN_TICK", "ramp-integer"): "M140 S90.",
                  # The ceiling is the whole point: above it, nothing engages.
                  # The ceiling withholds the CURTAIN only now: a draft across
                  # a 108 C plate is what it exists to prevent. Extraction is
                  # still allowed, and is asserted in `expect`.
                  ("gcode_macro _COOLDOWN_TICK", "assist-ceiling"): "SET_FAN_SPEED FAN=fan2",
                  # Engaging must never mean slamming to full duty.
                  ("gcode_macro _COOLDOWN_TICK", "assist-min-fan"): "exhaust_fan SPEED=1.0",
                  # THE COOLDOWN HAS NOTHING TO DO WITH CHAMBER CONTROL. None of
                  # these may claim ch_target, drive the chamber heater, or start
                  # or stop the chamber loop from inside the ramp.
                  ("gcode_macro _COOLDOWN_START", "heater"): "chamber_heater",
                  ("gcode_macro _COOLDOWN_START", "heaterless"): "SAVE_VARIABLE VARIABLE=ch_target",
                  ("gcode_macro _COOLDOWN_TICK", "ramp-heaterless"): "ch_target",
                  ("gcode_macro _COOLDOWN_TICK", "ramp-heater"): "chamber_heater",
                  ("gcode_macro _COOLDOWN_TICK", "chamber-target-ignored"): "_COOLDOWN_",
                  ("gcode_macro _COOLDOWN_TICK", "assist-drives-exhaust"): ["ch_target", "_CHAMBER_ON"],
                  ("gcode_macro _COOLDOWN_TICK", "assist-release-zeroes-exhaust"): ["ch_target", "_CHAMBER_OFF"],
                  # The vent timer writes nothing while a ramp owns the exhaust.
                  ("gcode_macro _CHAMBER_VENT", "ramp-owns-exhaust"): "SET_FAN_SPEED",
                  # The ratchet must not rise: no write of the minimum at all.
                  ("gcode_macro _BRUSH_MARK_DONE", "not-shortest"):
                      "VARIABLE=wipe_seconds_min",
                  ("gcode_macro _COOLDOWN_TICK", "assist-curtain-withheld"): "SET_FAN_SPEED FAN=fan2",
                  # A heater still holding the plate must not be overruled by a
                  # projection -- the 2026-09-13 early latch.
                  ("gcode_macro _COOLDOWN_TICK", "assist-lookahead-held"): "fan assist on",
                  ("gcode_macro _COOLDOWN_TICK", "assist-step-held"): "fan assist on",
                  # A nozzle hotter than the bed holds the settle: no ramp start.
                  ("gcode_macro _COOLDOWN_TICK", "settle-hold"): "_COOLDOWN_TAKE_OVER",
                  ("gcode_macro _COOLDOWN_TICK", "settle-progress"): "_COOLDOWN_TAKE_OVER",
                  ("gcode_macro _COOLDOWN_TICK", "assist-ignores-step-sawtooth"): "fan assist on",
                  ("gcode_macro _COOLDOWN_TICK", "curtain-holds-in-band"): "curtain fan off",
                  ("gcode_macro SET_CLEAN_TEMP", "bed-coming-down"): "M104 S195",
                  ("gcode_macro _PREFLIGHT", "no-hook"): "_SVZERO_PRINT_START_HOOK",
                  ("gcode_macro SET_CLEAN_TEMP", "exact"): "nozzle held",
                  ("gcode_macro _COOLDOWN_TICK", "ramp-first-tick"): "fan assist on",
                  ("gcode_macro _COOLDOWN_TICK", "assist-heater-holds-integral"): "VARIABLE=assist_i",
                  ("gcode_macro _COOLDOWN_TICK", "curtain-held-on-schedule"): ["curtain fan off", "VARIABLE=curtain_latched"],
                  ("gcode_macro _COOLDOWN_TICK", "ramp-same-degree"): ["bed 99C, about", "VARIABLE=last_degree", "VARIABLE=eta_last"],
                  ("gcode_macro _COOLDOWN_TICK", "ramp-progress-held-rate"): ["VARIABLE=eta_at", "VARIABLE=meas_rate", "bed 100C"],
                  # Anti-windup: a saturated tick banks nothing, so writes nothing.
                  ("gcode_macro _COOLDOWN_TICK", "assist-windup-held"): "VARIABLE=assist_i",
                  # Curtain is off by default, so the default assist scenario
                  # must never reach for fan2.
                  ("gcode_macro _COOLDOWN_TICK", "assist-engage"): "FAN=fan2",
                  ("gcode_macro _COOLDOWN_TICK", "ramp-tick-at-3c"): "M140",
                  ("gcode_macro _COOLDOWN_TICK", "ramp-overrun-full"): "Heaters off",
                  # A ramp with no chamber leg must never start the loop against
                  # the zero it carries -- that is a full-speed exhaust draft.
                  ("gcode_macro _COOLDOWN_START", "bed-only"): "_CHAMBER_ON",
                  # The new-print abort must not erase the policy that print
                  # armed for itself behind the same blocking wait.
                  ("gcode_macro _COOLDOWN_TICK", "abort-new-print"): "VARIABLE=policy_set VALUE=0",
                  ("gcode_macro _COOLDOWN_CLEAR", "keeps-policy"): "VARIABLE=policy_set VALUE=0",
                  # A release must not reach any abort path at all.
                  ("gcode_macro _COOLDOWN_TICK", "chamber-release-ignored"): ["_COOLDOWN_", "ceiling released"],
                  # Deferring must not touch ch_target on the way past.
                  ("gcode_macro M141", "ramp-owns-it"): "SAVE_VARIABLE",
                  # A zero floor must never arm the supervisor. vent_active is
                  # what puts the tach guard on a fan that is off on purpose.
                  ("gcode_macro _CHAMBER_VENT", "floor-zero-is-off"): "vent_active VALUE=1",
                  ("gcode_macro _CHAMBER_VENT", "floor-rounds-to-off"): "vent_active VALUE=1",
                  ("gcode_macro SET_LED", "index-masked"): ["WHITE=1.0", "WHITE=0.5"],
                  # A uniform chain gets ONE midpoint frame, not 33 index writes.
                  ("gcode_macro SET_LED", "split-down"): "INDEX=33",
                  # Staging only: no transmit, so there is nothing to split.
                  ("gcode_macro SET_LED", "no-transmit"): "WHITE=0.1 ",
                  ("gcode_macro SET_LED", "index-clear"): "INDEX=20",
                  # A renormalized call must not pass the raw value through.
                  ("gcode_macro SET_LED", "chain-over"): "WHITE=1.0",
                  ("gcode_macro SET_LED", "index-over"): "WHITE=1.0",
                  ("gcode_macro SET_LED", "chain"): "renormalized",
                  ("gcode_macro NOZZLE_LIGHT", "whole-chain"): "renormalized",
                  ("gcode_macro _LIGHT_APPLY_STATE", "tuning"): ["NOZZLE_LIGHT", "LIGHT_PATTERN", "SET_GCODE_VARIABLE"],
                  ("gcode_macro _LIGHT_APPLY_STATE", "unchanged"): ["NOZZLE_LIGHT", "LIGHT_PATTERN", "SET_GCODE_VARIABLE"],
                  ("gcode_macro _LIGHT_APPLY_STATE", "failed-retains"): ["NOZZLE_LIGHT", "LIGHT_PATTERN"],
                  ("gcode_macro _LIGHT_APPLY_STATE", "homed-selected"): "W=0.5",
                  ("gcode_macro SET_LED", "observer-printing"): "SET_LED_BASE",
                  ("gcode_macro SET_LED", "observer-paused"): "SET_LED_BASE",
                  ("gcode_macro SET_LED", "observer-unknown"): "SET_LED_BASE",
                  ("gcode_macro SET_LED", "observer-homed"): "SET_LED_BASE",
                  ("gcode_macro SET_LED", "observer-preheat"): "SET_LED_BASE",
                  ("gcode_macro SET_LED", "observer-tuning"): "SET_LED_BASE",
                  # The independent check on the solver. SET_LED costs the
                  # answer with the same constants and refuses to scale it, so
                  # a solve that overspends says so here without a meter.
                  ("gcode_macro NOZZLE_LIGHT", "max"): "renormalized",
                  ("gcode_macro NOZZLE_LIGHT", "max-starved"): "renormalized",
                  ("gcode_macro LIGHT_PATTERN", "counting"): "refused",
                  ("gcode_macro LIGHT_PATTERN", "over-budget"): "SET_LED",
                  ("gcode_macro _COOLDOWN_TICK", "inactive"): "SET_GCODE_VARIABLE",
                  ("gcode_macro END_PRINT", "cooldown-installed"): "TURN_OFF_HEATERS",
                  ("gcode_macro END_PRINT", "cooldown-absent"): "_COOLDOWN_START",
                  ("gcode_macro CANCEL_PRINT", "cooldown-absent"): "COOLDOWN_ABORT",
                  ("gcode_macro _CHAMBER_AUTOSTART", "cooldown-absent"): "COOLDOWN_ABORT",
                  # The whole point of the declined branch: the loop must not
                  # start, and no ceiling may be inferred behind the request.
                  ("gcode_macro _CHAMBER_AUTOSTART", "declined"): "_CHAMBER_ON",
                  ("gcode_macro _CHAMBER_AUTOSTART", "inferred"):
                      "as requested by the slicer",
                  ("gcode_macro M141", "declined"):
                      "VARIABLE=control_declined VALUE=0",
                  ("gcode_macro _CHAMBER_GUARD", "job-complete-cooling"): "_CHAMBER_OFF",
                  ("gcode_macro _CHAMBER_GUARD", "job-complete-no-cooldown"):
                      "UPDATE_DELAYED_GCODE ID=CHAMBER_GUARD"}
        # Scenarios that MUST raise, with text the error must carry. Any other
        # scenario that raises fails, and any of these that renders fails.
        refuse = {("gcode_macro SVZERO_REQUIRE", "manual-into-precision"):
                      "rename svzero-mode-precision.cfg.off to svzero-mode-precision.cfg",
                  ("gcode_macro SVZERO_REQUIRE", "manual-back-to-standard"):
                      "rename svzero-mode-precision.cfg to svzero-mode-precision.cfg.off",
                  ("gcode_macro SVZERO_REQUIRE", "precision-not-applied"):
                      "stepper_x runs 16 microsteps, not the 64 it sets",
                  ("gcode_macro M191", "r-cooling-without-python"):
                      "waiting for it to cool needs the Python preheat helper",
                  ("gcode_macro SVZERO_REQUIRE", "precision-job-standard-printer"):
                      "sliced for precision mode and the printer is in standard mode",
                  ("gcode_macro SVZERO_REQUIRE", "standard-job-precision-printer"):
                      "sliced for standard mode and the printer is in precision mode",
                  ("gcode_macro SVZERO_REQUIRE", "legacy-job-precision-printer"):
                      "sliced for standard mode and the printer is in precision mode",
                  ("gcode_macro SVZERO_REQUIRE", "precision-job-standard-printer-switch"):
                      "svzero_mode_switch restarts Klipper in precision mode and starts this job again",
                  ("gcode_macro SVZERO_REQUIRE", "standard-job-precision-printer-switch"):
                      "svzero_mode_switch restarts Klipper in standard mode and starts this job again",
                  ("gcode_macro _SVZERO_CHECK_PROFILE", "no-handshake-precision"):
                      "the printer is in precision mode, which runs only jobs sliced for it"}

        # Remote calls a scenario must make (a string) or must not make (None).
        remote = {("gcode_macro SVZERO_REQUIRE", "precision-job-standard-printer-switch"):
                      "svzero_mode_switch filename=job.gcode mode=precision",
                  ("gcode_macro SVZERO_REQUIRE", "standard-job-precision-printer-switch"):
                      "svzero_mode_switch filename=job.gcode mode=standard",
                  ("gcode_macro SVZERO_REQUIRE", "precision-job-standard-printer"): None,
                  ("gcode_macro SVZERO_REQUIRE", "mode-match-switch"): None,
                  ("gcode_macro SVZERO_MODE", "ask"): "svzero_mode_switch filename= mode=precision",
                  ("gcode_macro SVZERO_MODE", "off"): None,
                  ("gcode_macro SVZERO_MODE", "already"): None}

        # Everything defined above without a printer patch gets an empty one.
        if s == "gcode_macro CLEAN_NOZZLE":
            expect[(s, "cold-contact")] = "COLD CONTACT RUN"
            expect[(s, "dry")] = "DRY RUN"
            forbid[(s, "dry")] = "RUN_PROBE_PRESSURE"
        if s == "gcode_macro M191":
            for label in ("default-compatibility", "exact-minimum", "shadow-test-line"):
                expect[(s, label)] = "CHAMBER_PREHEAT_WAIT"
                forbid[(s, label)] = 'TEMPERATURE_WAIT SENSOR="temperature_sensor chamber_temp"'
            expect[(s, "without-python")] = 'TEMPERATURE_WAIT SENSOR="temperature_sensor chamber_temp" MINIMUM=30.0'
            forbid[(s, "without-python")] = "CHAMBER_PREHEAT_WAIT"
        scenarios = [sc if len(sc) == 6 else sc + ({},) for sc in scenarios]

        for label, scenario_params, scenario_rawparams, state_patch, extruder_patch, printer_patch in scenarios:
            suffix = " [%s]" % label if label else ""
            # Not every checked file is a chamber file; only the chamber
            # scenarios patch this object.
            state = printer.get("gcode_macro _CH_STATE", Mock())
            dump = os.environ.get("DUMP")
            old_state = {key: state.get(key) for key in state_patch}
            old_extruder = {key: printer.extruder.get(key) for key in extruder_patch}
            # A printer patch may ADD an object the base mock lacks (the
            # chamber heater) or REMOVE one it has, spelled None (cooldown.cfg
            # not installed). Both directions have to be undone afterwards.
            old_printer = {k: printer[k] for k in printer_patch if k in printer}
            added = [k for k in printer_patch if k not in printer]
            try:
                state.update(state_patch)
                printer.extruder.update(extruder_patch)
                for k, v in printer_patch.items():
                    if v is None:
                        printer.pop(k, None)
                    else:
                        printer[k] = v
                del remote_calls[:]
                effective = scenario_params
                if effective is None:
                    effective = klipper_params(
                        s.split()[1].upper(), scenario_rawparams
                    )
                # DUMP=<label> prints the rendered G-code for one scenario.
                # Added because the expect table names exact duties, and
                # deriving those by hand and guessing at the rounding is how
                # wrong numbers get enshrined as expectations.
                out = env.from_string(cp.get(s, 'gcode')).render(
                    printer=printer, params=effective,
                    rawparams=scenario_rawparams
                )
                if dump and dump in (label, s, "all"):
                    print("   --- %s%s ---" % (s, suffix))
                    for ln in out.splitlines():
                        if ln.strip():
                            print("   | %s" % ln.rstrip())
                if (s, label) in refuse:
                    bad += 1
                    print("   FAIL    %s%s -> rendered, but this job must be refused"
                          % (s, suffix))
                    continue
                bad_chars = [(ln.strip()[:70], c)
                             for ln in out.splitlines() if "MSG=" in ln
                             for c in ";#*" if c in ln.split("MSG=", 1)[1]]
                if bad_chars:
                    bad += 1
                    for ln, c in bad_chars[:2]:
                        print("   FAIL    %s%s -> %r in a RESPOND arg: %s"
                              % (s, suffix, c, ln))
                    continue
                # A list asserts EVERY entry. The blended cooldown throttle is
                # the case that forced it: "the exhaust stepped down" and "the
                # curtain came on at its floor" are one claim about one tick,
                # and either half alone is satisfied by the wrong branch.
                want = expect.get((s, label))
                if want is not None:
                    miss = [w for w in expected_tokens(want)
                            if w not in out]
                    if miss:
                        bad += 1
                        print("   FAIL    %s%s -> took the wrong branch, no %s in output"
                              % (s, suffix, ", ".join(repr(m) for m in miss)))
                        continue
                never = forbid.get((s, label))
                if never is not None:
                    hit = [n for n in expected_tokens(never)
                           if n in out]
                    if hit:
                        bad += 1
                        print("   FAIL    %s%s -> took the wrong branch, %s present"
                              % (s, suffix, ", ".join(repr(h) for h in hit)))
                        continue
                if not remote_ok(s, label, suffix):
                    bad += 1
                    continue
                print("   ok      %s%s" % (s, suffix))
            except Exception as e:
                # action_raise_error throws RuntimeError (see the env setup), so
                # a refusal is asserted by its message, never by any exception.
                why = refuse.get((s, label))
                if why is not None and isinstance(e, RuntimeError) and why in str(e):
                    if not remote_ok(s, label, suffix):
                        bad += 1
                        continue
                    print("   ok      %s%s (refused)" % (s, suffix))
                    continue
                bad += 1
                print("   FAIL    %s%s -> %s: %s"
                      % (s, suffix, type(e).__name__, str(e)[:90]))
            finally:
                state.update(old_state)
                printer.extruder.update(old_extruder)
                printer.update(old_printer)
                for k in added:
                    printer.pop(k, None)
    return bad


if __name__ == "__main__":
    files = sys.argv[1:] or [os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                            "klipper", "config", name)
                                for name in ("nozzle_brush.cfg", "purge_line.cfg")]
    total = 0
    for f in files:
        print("=== %s ===" % f)
        total += check(f)
    print("\n%s" % ("all macros render" if not total else "%d FAILED" % total))
    sys.exit(1 if total else 0)
