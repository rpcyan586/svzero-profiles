#!/usr/bin/env python3
"""Parse AND render every macro in a Klipper .cfg, in Klipper's own environment.

    ./tools/check-macros.py nozzle_brush.cfg purge_line.cfg

Two lessons are baked in, both learned the expensive way:

* **Klipper's environment, exactly.** jinja2.Environment('{%', '%}', '{', '}')
  with NO extensions (gcode_macro.py:74). An earlier harness enabled
  jinja2.ext.do, accepted a {% do %} tag Klipper does not have, and the resulting
  config HALTED THE PRINTER. A validator that is more permissive than the target
  converts a syntax error into confidence.

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
                             filament_sensor_print=False))
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
        "configfile": Mock(settings=Mock(extruder=Mock(min_extrude_temp=150))),
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
        for label, scenario_params, scenario_rawparams, state_patch, extruder_patch in scenarios:
            suffix = " [%s]" % label if label else ""
            state = printer["gcode_macro _CH_STATE"]
            old_state = {key: state.get(key) for key in state_patch}
            old_extruder = {key: printer.extruder.get(key) for key in extruder_patch}
            try:
                state.update(state_patch)
                printer.extruder.update(extruder_patch)
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
                print("   ok      %s%s" % (s, suffix))
            except Exception as e:
                bad += 1
                print("   FAIL    %s%s -> %s: %s"
                      % (s, suffix, type(e).__name__, str(e)[:90]))
            finally:
                state.update(old_state)
                printer.extruder.update(old_extruder)
    return bad


if __name__ == "__main__":
    files = sys.argv[1:] or ["nozzle_brush.cfg", "purge_line.cfg"]
    total = 0
    for f in files:
        print("=== %s ===" % f)
        total += check(f)
    print("\n%s" % ("all macros render" if not total else "%d FAILED" % total))
    sys.exit(1 if total else 0)
