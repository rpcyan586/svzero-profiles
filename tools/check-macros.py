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
import ast, configparser, sys, jinja2


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


def check(path):
    env = jinja2.Environment('{%', '%}', '{', '}', undefined=jinja2.StrictUndefined)
    env.globals["action_raise_error"] = lambda m: (_ for _ in ()).throw(RuntimeError(m))
    cp = configparser.RawConfigParser(strict=False, inline_comment_prefixes=('#', ';'))
    cp.read_string('\n'.join(l.split('#')[0] for l in open(path, encoding="utf-8")))

    settings = {s: Mock({k[9:]: ast.literal_eval(v.strip())
                         for k, v in cp.items(s) if k.startswith("variable_")})
                for s in cp.sections() if s.startswith("gcode_macro _")}
    # Shared macro files may depend on a small hardware-identity include that is
    # validated separately. Supply the same public interface here so the common
    # controller can still be rendered as a standalone source file.
    settings.setdefault("gcode_macro _CH_HW", Mock(fan="exhaust_fan"))
    printer = Mock(settings)
    printer.update({
        "exclude_object": Mock(objects=[Mock(polygon=box(60, 60, 90, 90))]),
        "toolhead": Mock(
            max_accel=5000.0, homed_axes="xyz", position=[0, 0, 0, 0],
            axis_minimum=Mock(x=0.0, y=0.0, z=0.0),
            axis_maximum=Mock(x=200.0, y=200.0, z=250.0),
        ),
        "save_variables": Mock(variables=Mock(nb_lane=0, ch_target=32.0, ch_kp=0.15)),
        "gcode_move": Mock(absolute_extrude=False, absolute_coordinates=True,
                           gcode_position=Mock(x=0.0, y=0.0, z=0.42),
                           position=Mock(x=0.0, y=0.0, z=0.42)),
        "extruder": Mock(temperature=25.0, target=0.0),
        "heater_bed": Mock(temperature=25.0, target=0.0),
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
        try:
            out = env.from_string(cp.get(s, 'gcode')).render(
                printer=printer, params=params, rawparams="S40"
            )
            bad_chars = [(ln.strip()[:70], c)
                         for ln in out.splitlines() if "MSG=" in ln
                         for c in ";#*" if c in ln.split("MSG=", 1)[1]]
            if bad_chars:
                bad += 1
                for ln, c in bad_chars[:2]:
                    print("   FAIL    %s -> %r in a RESPOND arg: %s" % (s, c, ln))
                continue
            print("   ok      %s" % s)
        except Exception as e:
            bad += 1
            print("   FAIL    %s -> %s: %s" % (s, type(e).__name__, str(e)[:90]))
    return bad


if __name__ == "__main__":
    files = sys.argv[1:] or ["nozzle_brush.cfg", "purge_line.cfg"]
    total = 0
    for f in files:
        print("=== %s ===" % f)
        total += check(f)
    print("\n%s" % ("all macros render" if not total else "%d FAILED" % total))
    sys.exit(1 if total else 0)
