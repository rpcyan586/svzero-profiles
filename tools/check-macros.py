#!/usr/bin/env python3
"""Parse AND render every macro in a Klipper .cfg, in Klipper's own environment.

    ./tools/check-macros.py nozzle_brush.cfg purge_line.cfg

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
  PURGE_STATUS lesson below, one level up, and it was live here — a
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
  shipped from this repo -- PURGE_STATUS kept referencing a removed `volume`
  through several commits because only PURGE_LINE was ever rendered.

Missing printer state resolves to Undefined rather than raising, matching how
Klipper behaves with `|default(...)`. Anything else is StrictUndefined, so a
genuine typo fails loudly.
"""
import ast, configparser, re, shlex, sys, jinja2


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
    a letter then a digit, so M191 but not CHAMBER_ON -- never receives
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
    for _fn in ("action_respond_info", "action_emergency_stop", "action_call_remote_method"):
        env.globals[_fn] = lambda *a, **k: ""
    cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=('#', ';'))
    cp.read_string('\n'.join(l.split('#')[0] for l in open(path, encoding="utf-8")))

    settings = {s: Mock({k[9:]: ast.literal_eval(v.strip())
                         for k, v in cp.items(s) if k.startswith("variable_")})
                for s in cp.sections() if s.startswith("gcode_macro _")}
    # Shared macro files may depend on a small hardware-identity include that is
    # validated separately. Supply the same public interface here so the common
    # controller can still be rendered as a standalone source file.
    settings.setdefault("gcode_macro _CH_HW", Mock(fan="exhaust_fan"))
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
    settings.setdefault("gcode_macro _BRUSH",
                        Mock(retracted=3.0, owed_max=10.0, retract_f=2700))
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
                             idle_restore=1800.0, coast_warned=0))
    settings.setdefault("gcode_macro _COOLDOWN_CFG",
                        Mock(final=35.0, chamber_rate=1.5, bed_rate=5.0,
                             chamber_step=1.0, deadband=2.0, settle_min=180,
                             settle_max=600, settle_slope=0.5, settle_tick=15,
                             park_z=100.0, max_seconds=5400, coast_delta=5.0,
                             override_delta=0.5, idle_margin=300))
    # cooldown.cfg reads the chamber loop's own state -- `running` to decide
    # whether it must restart the loop, `chamber_slope` to gate the settle.
    settings.setdefault("gcode_macro _CH_STATE",
                        Mock(running=1, chamber_slope=0.1))
    printer = Mock(settings)
    printer.update({
        "exclude_object": Mock(objects=[Mock(polygon=box(60, 60, 90, 90))]),
        "toolhead": Mock(
            max_accel=5000.0, homed_axes="xyz", position=[0, 0, 0, 0],
            estimated_print_time=123.456,
            axis_minimum=Mock(x=0.0, y=0.0, z=0.0),
            axis_maximum=Mock(x=200.0, y=200.0, z=250.0),
        ),
        "save_variables": Mock(variables=Mock(nb_lane=0, ch_target=32.0, ch_kp=0.15,
                                              end_retract=0.0)),
        "filament_switch_sensor filament_sensor": Mock(enabled=True,
                                                       filament_detected=True),
        # COOLDOWN_START reads idle_timeout.timeout to know what to restore.
        "configfile": Mock(settings=Mock(extruder=Mock(min_extrude_temp=150),
                                         idle_timeout=Mock(timeout=1800.0))),
        "gcode_move": Mock(absolute_extrude=False, absolute_coordinates=True,
                           gcode_position=Mock(x=0.0, y=0.0, z=0.42),
                           position=Mock(x=0.0, y=0.0, z=0.42)),
        "extruder": Mock(temperature=25.0, target=0.0, power=0.0),
        "heater_bed": Mock(temperature=25.0, target=65.0),
        "temperature_sensor chamber_temp": Mock(temperature=30.0),
        "temperature_sensor Toolhead_Temp": Mock(temperature=30.0),
        "fan_generic exhaust_fan": Mock(speed=0.0, rpm=1500.0),
        "print_stats": Mock(state="standby", info=Mock(current_layer=None)),
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
        if s == "gcode_macro _CH_PROXY_ANCHOR":
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
            ]
        elif s == "gcode_macro M191":
            scenarios = [
                ("default-compatibility", None, "", {}, {}),
                ("exact-minimum", None, "S30", {}, {}),
                # The real published line. Every KEY=VALUE here reaches the
                # macro with a literal "=" still attached.
                ("shadow-test-line", None,
                 "S33 Z15 STIR=1 BOOST=1 MAX_BOOST=10 COAST_RATE=1"
                 " INITIAL_BOOST=5 FILTER_ALPHA=0.25",
                 {}, {}),
                ("abort", None, "S0", {}, {}),
            ]

        # cooldown.cfg carries two actuator models and four phases, and an
        # unrendered branch is an unvalidated one -- the PURGE_STATUS lesson in
        # the docstring above. These scenarios patch the `printer` object
        # itself, which no earlier scenario needed: the chamber heater's
        # presence IS the branch, and _COOL_STATE has to be replaced wholesale
        # to walk the phases.
        heater = {"heater_generic chamber_heater": Mock(temperature=58.0,
                                                        target=60.0)}
        hot_bed = {"heater_bed": Mock(temperature=108.0, target=110.0)}

        def cool(ch=None, **kw):
            """A _COOL_STATE mid-ramp on the PC policy: 110/60 to 35 at 2.5.

            ch_target is kept consistent with c_cmd by default, because
            _COOLDOWN_TICK aborts on a mismatch -- so a scenario that left the
            base mock's 32 C in place would silently test only the abort path
            and never render the ramp. Pass `ch` to test the mismatch on
            purpose.
            """
            base = dict(active=1, phase=2, policy_set=1, p_final=35.0,
                        p_chamber_rate=1.0, p_bed_rate=2.5,
                        c0=60.0, b0=110.0, c_cmd=60.0, b_cmd=110.0,
                        c_step=1.0, b_step=3.0, interval=72.0,
                        has_heater=0, elapsed=600, settle_elapsed=180,
                        idle_restore=1800.0, coast_warned=0)
            base.update(kw)
            if ch is None:
                ch = base["c_cmd"] + (2.0 if base["has_heater"] else 0.0)
            patch = {"gcode_macro _COOL_STATE": Mock(base),
                     "save_variables": Mock(variables=Mock(ch_target=ch))}
            patch.update(hot_bed)
            return patch

        if s == "gcode_macro COOLDOWN_START":
            scenarios = [
                ("heaterless", params, "", {}, {}, hot_bed),
                ("heater", params, "", {}, {}, dict(hot_bed, **heater)),
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
        elif s == "gcode_macro _COOLDOWN_TICK":
            scenarios = [
                ("inactive", params, "", {}, {}, cool(active=0)),
                ("settle-hold", params, "", {}, {},
                 cool(phase=1, settle_elapsed=0, elapsed=0)),
                ("settle-flat", params, "", {}, {},
                 cool(phase=1, settle_elapsed=180, elapsed=180)),
                ("ramp-heaterless", params, "", {}, {}, cool()),
                ("ramp-heater", params, "", {}, {},
                 dict(cool(has_heater=1), **heater)),
                ("ramp-last-step", params, "", {}, {},
                 cool(c_cmd=35.5, b_cmd=110.0,
                      **{"b_step": 200.0})),
                ("abort-bed-override", params, "", {}, {}, cool(b_cmd=42.0)),
                ("abort-chamber-override", params, "", {}, {}, cool(ch=48.0)),
                ("watchdog", params, "", {}, {}, cool(elapsed=99999)),
                ("abort-new-print", params, "", {}, {},
                 dict(cool(), **{"print_stats":
                                 Mock(state="printing",
                                      info=Mock(current_layer=None))})),
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
                   "gcode_macro CHAMBER_AUTOSTART"):
            # These three reach cooldown.cfg through `in printer`, so both
            # sides of that guard have to render.
            scenarios = [
                ("cooldown-installed", params, "S40", {}, {}, {}),
                ("cooldown-running", params, "S40", {}, {}, cool()),
                ("cooldown-absent", params, "S40", {}, {},
                 {"gcode_macro _COOL_STATE": None,
                  "gcode_macro _COOLDOWN_CFG": None}),
            ]

        # An optional 7th element: a string the rendered output MUST contain.
        # Jinja only evaluates the branch it takes, so a scenario that quietly
        # lands in the wrong one renders fine and validates nothing -- the
        # branch it was written for is still unrendered. Naming the expected
        # output turns "it rendered" into "it rendered THIS".
        expect = {
            ("gcode_macro COOLDOWN_START", "heaterless"): "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER",
            ("gcode_macro COOLDOWN_START", "heater"): "HEATER=chamber_heater",
            ("gcode_macro COOLDOWN_START", "bed-only"): "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER",
            ("gcode_macro COOLDOWN_START", "nothing-to-do"): "nothing to ramp",
            ("gcode_macro _COOLDOWN_TICK", "settle-hold"): "DURATION=15",
            ("gcode_macro _COOLDOWN_TICK", "settle-flat"): "starting ramp",
            ("gcode_macro _COOLDOWN_TICK", "ramp-heaterless"): "SAVE_VARIABLE VARIABLE=ch_target",
            ("gcode_macro _COOLDOWN_TICK", "ramp-heater"): "HEATER=chamber_heater",
            ("gcode_macro _COOLDOWN_TICK", "ramp-last-step"): "ramp complete",
            ("gcode_macro _COOLDOWN_TICK", "abort-bed-override"): "bed target changed",
            ("gcode_macro _COOLDOWN_TICK", "abort-chamber-override"): "chamber ceiling changed",
            ("gcode_macro _COOLDOWN_TICK", "watchdog"): "watchdog",
            ("gcode_macro _COOLDOWN_TICK", "abort-new-print"): "a new print has started",
            ("gcode_macro END_PRINT", "cooldown-installed"): "COOLDOWN_START",
            ("gcode_macro END_PRINT", "cooldown-absent"): "TURN_OFF_HEATERS",
            ("gcode_macro CANCEL_PRINT", "cooldown-installed"): "COOLDOWN_ABORT",
            ("gcode_macro _CHAMBER_GUARD", "job-complete-no-cooldown"): "CHAMBER_OFF",
            ("gcode_macro _CHAMBER_GUARD", "job-complete-cooling"): "UPDATE_DELAYED_GCODE ID=CHAMBER_GUARD",
            ("gcode_macro _CHAMBER_GUARD", "cooldown-absent"): "CHAMBER_OFF",
            ("gcode_macro CHAMBER_AUTOSTART", "cooldown-installed"): "COOLDOWN_ABORT",
        }
        # The inactive tick must produce nothing at all.
        forbid = {("gcode_macro _COOLDOWN_TICK", "inactive"): "SET_GCODE_VARIABLE",
                  ("gcode_macro END_PRINT", "cooldown-installed"): "TURN_OFF_HEATERS",
                  ("gcode_macro END_PRINT", "cooldown-absent"): "COOLDOWN_START",
                  ("gcode_macro CANCEL_PRINT", "cooldown-absent"): "COOLDOWN_ABORT",
                  ("gcode_macro CHAMBER_AUTOSTART", "cooldown-absent"): "COOLDOWN_ABORT",
                  ("gcode_macro _CHAMBER_GUARD", "job-complete-cooling"): "CHAMBER_OFF",
                  ("gcode_macro _CHAMBER_GUARD", "job-complete-no-cooldown"):
                      "UPDATE_DELAYED_GCODE ID=CHAMBER_GUARD"}

        # Everything defined above without a printer patch gets an empty one.
        scenarios = [sc if len(sc) == 6 else sc + ({},) for sc in scenarios]

        for label, scenario_params, scenario_rawparams, state_patch, extruder_patch, printer_patch in scenarios:
            suffix = " [%s]" % label if label else ""
            # Not every checked file is a chamber file; only the chamber
            # scenarios patch this object.
            state = printer.get("gcode_macro _CH_STATE", Mock())
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
                effective = scenario_params
                if effective is None:
                    effective = klipper_params(
                        s.split()[1].upper(), scenario_rawparams
                    )
                out = env.from_string(cp.get(s, 'gcode')).render(
                    printer=printer, params=effective,
                    rawparams=scenario_rawparams
                )
                bad_chars = [(ln.strip()[:70], c)
                             for ln in out.splitlines() if "MSG=" in ln
                             for c in ";#*" if c in ln.split("MSG=", 1)[1]]
                if bad_chars:
                    bad += 1
                    for ln, c in bad_chars[:2]:
                        print("   FAIL    %s%s -> %r in a RESPOND arg: %s"
                              % (s, suffix, c, ln))
                    continue
                want = expect.get((s, label))
                if want is not None and want not in out:
                    bad += 1
                    print("   FAIL    %s%s -> took the wrong branch, no %r in output"
                          % (s, suffix, want))
                    continue
                never = forbid.get((s, label))
                if never is not None and never in out:
                    bad += 1
                    print("   FAIL    %s%s -> took the wrong branch, %r present"
                          % (s, suffix, never))
                    continue
                print("   ok      %s%s" % (s, suffix))
            except Exception as e:
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
    files = sys.argv[1:] or ["nozzle_brush.cfg", "purge_line.cfg"]
    total = 0
    for f in files:
        print("=== %s ===" % f)
        total += check(f)
    print("\n%s" % ("all macros render" if not total else "%d FAILED" % total))
    sys.exit(1 if total else 0)
