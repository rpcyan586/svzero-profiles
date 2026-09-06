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
                             c_rate=0.83, b_rate=2.5, total_s=1800.0,
                             last_report=0,
                             idle_restore=1800.0, coast_warned=0,
                             material="default", rates_set=0, assist=0,
                             assist_curtain=0.0, assist_exhaust=0.0,
                             ch_written=32.0))
    settings.setdefault("gcode_macro _COOLDOWN_CFG",
                        Mock(final=35.0, chamber_rate=1.5, bed_rate=5.0,
                             chamber_step=1.0, deadband=2.0, settle_min=180,
                             settle_max=600, settle_slope=0.5, settle_tick=15,
                             park_z=100.0, max_seconds=5400, coast_delta=5.0,
                             override_delta=0.5, idle_margin=300,
                             ramp_tick=15, report_seconds=60))
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
        "save_variables": Mock(variables=Mock(was_interrupted=0.0,
                                         nb_lane=0, ch_target=32.0, ch_kp=0.15,
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
        "print_stats": Mock(state="standby", filename="job.gcode",
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
        if s == "gcode_macro SET_CLEAN_TEMP":
            # The table moved out of the slicer's G-code on 2026-09-05, so the
            # lookup is now ours to get right rather than the emitter's.
            scenarios = [
                ("exact", Mock(MATERIAL="ABS"), "MATERIAL=ABS", {}, {}),
                # Prefix match over `order`: "PLA+" must land on pla.
                ("prefix", Mock(MATERIAL="PLA+"), "MATERIAL=PLA+", {}, {}),
                # Unknown material takes Sovol's flat 200, not a guess.
                ("fallback", Mock(MATERIAL="Nylon"), "MATERIAL=Nylon", {}, {}),
                ("explicit", Mock(MATERIAL="ABS", TEMP="150"),
                 "MATERIAL=ABS TEMP=150", {}, {}),
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
            ]
        elif s == "gcode_macro CHAMBER_STIR_ORBIT":
            scenarios = [
                # Shipped defaults on a 152.4 mm plate: 26 rings inside 60 s.
                ("default", params, "", {}, {}),
                ("default2", params, "", {}, {}),
                # A budget that cannot fit even the first ring must say so
                # rather than emitting a partial sweep.
                # 0.5 s at 6000 mm/min allows 50 mm; the first ring alone needs
                # 2*pi*10 + step + the straight return = 74.8 mm. BUDGET=1 was
                # the first guess and it FITS one ring, which is the macro being
                # right and the test being wrong.
                ("no-budget", Mock(BUDGET="0.5"), "BUDGET=0.5", {}, {}),
                # Edge clamp: a huge R_MAX must still be cut to the reach.
                ("clamped", Mock(R_MAX="500"), "R_MAX=500", {}, {}),
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
            pack = lambda seen=0: {"gcode_macro _SVZERO_PACK":
                                   Mock(version="1.0.4", seen=seen)}
            scenarios = [
                ("version-match", Mock(VERSION="1.0.4"), "VERSION=1.0.4",
                 {}, {}, pack()),
                ("version-mismatch", Mock(VERSION="0.9.0"), "VERSION=0.9.0",
                 {}, {}, pack()),
            ]
        elif s == "gcode_macro _SVZERO_CHECK_PROFILE":
            scenarios = [
                ("no-handshake", params, "", {}, {},
                 {"gcode_macro _SVZERO_PACK": Mock(version="1.0.4", seen=0)}),
                ("handshake-seen", params, "", {}, {},
                 {"gcode_macro _SVZERO_PACK": Mock(version="1.0.4", seen=1)}),
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

        def cool(ch=None, bed_temp=None, **kw):
            """A _COOL_STATE mid-ramp on the PC policy: 110/60 to 35 at 2.5.

            ch_target is kept consistent with c_cmd by default, because
            _COOLDOWN_TICK aborts on a mismatch -- so a scenario that left the
            base mock's 32 C in place would silently test only the abort path
            and never render the ramp. Pass `ch` to test the mismatch on
            purpose.
            """
            # Rates and total, as COOLDOWN_START would derive them for this
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
                        ch_written=0.0)
            # `b_at` places the scenario on the tick that commands this bed
            # setpoint, by solving the schedule for elapsed. Assist cases care
            # about where the bed is RELATIVE to its commanded value, and since
            # the commanded value is now a function of the clock, setting it
            # directly would describe an impossible state.
            b_at = kw.pop("b_at", None)
            base.update(kw)
            if b_at is not None:
                base["elapsed"] = int(base["settle_elapsed"]
                                      + (base["b0"] - b_at) / base["b_rate"] * 60.0
                                      - 15)
            # THE COMMANDED PAIR IS DERIVED FROM ELAPSED, not carried
            # independently. Since 2026-09-05 the macro computes its setpoints
            # as c0 - rate * t, so a scenario that set elapsed and c_cmd to
            # unrelated values would describe a state the ramp can never be in
            # -- and, worse, would still render. Anything the scenario passes
            # explicitly still wins, which is how the override cases work.
            _ramp = max(0, base["elapsed"] - base["settle_elapsed"]) if base["phase"] == 2 else 0
            for _k, _r, _z in (("c_cmd", base["c_rate"], base["c0"]),
                               ("b_cmd", base["b_rate"], base["b0"])):
                if _k not in kw:
                    base[_k] = float(max(base["p_final"], round(_z - _r * _ramp / 60.0)))
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
            patch = {"gcode_macro _COOL_STATE": Mock(base),
                     "save_variables": Mock(variables=Mock(ch_target=ch))}
            # The bed the ramp believes it commanded, coasting a little above
            # it. A fixed 110 here would trip the bed-override abort in every
            # scenario whose b_cmd has moved on. Scenarios that want the
            # mismatch, or a specific bed temperature, still pass heater_bed.
            patch["heater_bed"] = Mock(
                temperature=base["b_cmd"] + 8.0 if bed_temp is None else bed_temp,
                target=base["b_cmd"])
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
        elif s == "gcode_macro _CHAMBER_VENT":
            # This supervisor had NO scenarios, so its branches were never
            # rendered. It is also the macro that can drive the exhaust to 1.0.
            hot = {"extruder": Mock(temperature=250.0, target=250.0, power=0.5),
                   "fan_generic exhaust_fan": Mock(speed=0.0, rpm=0.0)}
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
                ("settle-hold", params, "", {}, {},
                 cool(phase=1, settle_elapsed=0, elapsed=0)),
                ("settle-flat", params, "", {}, {},
                 cool(phase=1, settle_elapsed=180, elapsed=180)),
                # The settle phase used to say NOTHING for up to ten minutes,
                # which read as a hung cooldown. A still-moving chamber holds
                # the phase open, so this must report rather than fall silent.
                ("settle-progress", params, "", {}, {},
                 dict(cool(phase=1, settle_elapsed=300, elapsed=300),
                      **{"gcode_macro _CH_STATE":
                          Mock(running=1, chamber_slope=-1.2)})),
                ("ramp-heaterless", params, "", {}, {}, cool()),
                # Whole-degree commands, and a progress line with an ETA.
                ("ramp-integer", params, "", {}, {}, cool()),
                ("ramp-progress", params, "", {}, {}, cool(last_report=0)),
                ("ramp-heater", params, "", {}, {},
                 dict(cool(has_heater=1), **heater)),
                # The endpoint is reached by ELAPSED now: settle 180 plus the
                # full 1800 s schedule, so both axes snap to final together.
                ("ramp-last-step", params, "", {}, {}, cool(elapsed=1980)),
                ("abort-bed-override", params, "", {}, {},
                 dict(cool(), **{"heater_bed": Mock(temperature=108.0,
                                                    target=42.0)})),
                ("abort-chamber-override", params, "", {}, {},
                 cool(ch=48.0, ch_written=60.0)),
                # M141 S0 -- a RELEASE, which slicers emit at end of job. It
                # must drop the chamber leg and keep ramping the bed, not abort.
                ("chamber-released", params, "", {}, {},
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
                # Just past error_on: the exhaust must come up at the stall
                # floor, NOT at full. err 1.5, over 0.5 of a 5 C band, so
                # 0.13 + 0.87 * 0.1 = 0.217.
                ("assist-min-fan", params, "", {}, {},
                 cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=46.5)),
                # Same small error with the curtain ENABLED: it must still stay
                # out, because the exhaust is nowhere near spent.
                ("assist-curtain-withheld", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=46.5),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                             Mock(exhaust_enable=1, curtain_enable=1,
                                  error_on=1.0, error_off=0.3,
                                  exhaust_span=5.0, exhaust_min=0.0,
                                  exhaust_max=1.0, curtain_span=5.0,
                                  curtain_max=0.45, bed_ceiling=60.0)})),
                # Same error, bed ABOVE the ceiling. Must NOT engage: this is
                # the gate that keeps forced air off a part that is still hot.
                ("assist-ceiling", params, "", {}, {},
                 dict(cool(b_at=95.0, c_step=0.0, c_rate=0.0, bed_temp=108.0),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                             Mock(exhaust_enable=1, curtain_enable=1,
                                  error_on=1.0, error_off=0.3,
                                  exhaust_span=5.0, exhaust_min=0.0,
                                  exhaust_max=1.0, curtain_span=5.0,
                                  curtain_max=0.45, bed_ceiling=60.0)})),
                # Latched on, error now inside the release band. Must release
                # rather than hold, or the fans never stop.
                ("assist-release", params, "", {}, {},
                 cool(b_at=54.0, c_step=0.0, c_rate=0.0, assist=1,
                      assist_curtain=0.3, bed_temp=52.0)),
                # Curtain explicitly enabled: the fan write must appear.
                ("assist-curtain", params, "", {}, {},
                 dict(cool(b_at=45.0, c_step=0.0, c_rate=0.0, bed_temp=55.0),
                      **{"gcode_macro _COOLDOWN_ASSIST_CFG":
                             Mock(exhaust_enable=1, curtain_enable=1,
                                  error_on=1.0, error_off=0.3,
                                  exhaust_span=5.0, exhaust_min=0.0,
                                  exhaust_max=1.0, curtain_span=5.0,
                                  curtain_max=0.45, bed_ceiling=60.0)})),
                # Bed-only ramp -- c_step 0, no chamber ceiling to lower. The
                # assist must claim one outright rather than min() against the
                # zero that means "no chamber control".
                ("assist-bed-only-claims-ceiling", params, "", {}, {},
                 cool(b_at=45.0, c_cmd=0.0, c_step=0.0, c_rate=0.0, ch=0.0,
                      ch_written=0.0, bed_temp=55.0)),
                # Latched on with a claimed ceiling, error now inside the release
                # band: it must hand the ceiling back, not leave the exhaust
                # venting to a stale setpoint for the rest of the ramp.
                ("assist-bed-only-releases-ceiling", params, "", {}, {},
                 cool(b_at=54.0, c_cmd=0.0, c_step=0.0, c_rate=0.0, assist=1,
                      ch=32.0, ch_written=32.0, bed_temp=52.0)),
                # No chamber sensor at all. The exhaust half must sit down
                # quietly rather than commanding a ceiling off a missing read.
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
            if s == "gcode_macro CHAMBER_AUTOSTART":
                # The three ways a print can arrive here, which differ only in
                # _CH_STATE and previously all ended in the same CHAMBER_ON.
                scenarios += [
                    ("declined", params, "S40",
                     {"requested": 1, "control_declined": 1}, {}, {}),
                    ("slicer-target", params, "S40",
                     {"requested": 1, "control_declined": 0}, {}, {}),
                    ("inferred", params, "S40",
                     {"requested": 0, "control_declined": 0}, {}, {}),
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
        if s == "gcode_macro SET_LED":
            scenarios = [
                ("other-led", None, "LED=Screen_Colour RED=1", {}, {}, {}),
                ("chain", None, "LED=nozzle_light WHITE=0.2", {}, {}, {}),
                ("index-masked", None, "LED=nozzle_light WHITE=1 INDEX=20",
                 {}, {}, {}),
                ("index-clear", None, "LED=nozzle_light WHITE=1 INDEX=7",
                 {}, {}, {}),
                ("chain-over", None, "LED=nozzle_light WHITE=1", {}, {}, starved),
                ("index-over", None, "LED=nozzle_light WHITE=1 INDEX=7",
                 {}, {}, starved),
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
            ("gcode_macro START_PRINT", "load-cell"): "METHOD=force_overlay",
            # Arcs, not straight-line approximations, and a full circle has no
            # endpoint so gcode_arcs takes its 2*pi branch.
            ("gcode_macro CHAMBER_STIR_ORBIT", "default"): "G2 I-10.0 J0",
            # 26 rings from 10 to 60 mm at a 2 mm step, the largest sweep that
            # fits 60 s at 6000 mm/min.
            ("gcode_macro CHAMBER_STIR_ORBIT", "default2"): "26 rings 10-60mm",
            ("gcode_macro CHAMBER_STIR_ORBIT", "no-budget"): "too small for even one ring",
            ("gcode_macro SET_CLEAN_TEMP", "exact"): "M104 S195",
            ("gcode_macro _BRUSH_INFER_CLEAN_TEMP", "from-spool"): "SET_CLEAN_TEMP MATERIAL=ABS",
            ("gcode_macro _BRUSH_INFER_CLEAN_TEMP", "from-last-print"): "SET_CLEAN_TEMP TEMP=190",
            ("gcode_macro _BRUSH_INFER_CLEAN_TEMP", "blind"): "no print history",
            ("gcode_macro SET_CLEAN_TEMP", "prefix"): "M104 S170",
            ("gcode_macro SET_CLEAN_TEMP", "fallback"): "M104 S200",
            ("gcode_macro SET_CLEAN_TEMP", "explicit"): "M104 S150",
            # The handshake WARNS and never refuses, in both directions.
            ("gcode_macro SVZERO_REQUIRE", "version-mismatch"): "profile expects pack 0.9.0",
            ("gcode_macro _SVZERO_CHECK_PROFILE", "no-handshake"): "not sliced by an rpcyan SV Zero profile",
            ("gcode_macro _COOLDOWN_TICK", "settle-progress"): "settling 315/600s",
            # The bed setpoint is commanded in whole degrees: at 460 -> 475 s of
            # a 2.5 C/min bed leg from 110 the line sits at 90.21, which must be
            # commanded as 90 and not as 90.21.
            ("gcode_macro _COOLDOWN_TICK", "ramp-integer"): "M140 S90",
            ("gcode_macro _COOLDOWN_TICK", "ramp-progress"): "cooling to 35C",
            ("gcode_macro _COOLDOWN_TICK", "ramp-heaterless"): "SAVE_VARIABLE VARIABLE=ch_target",
            ("gcode_macro _COOLDOWN_TICK", "ramp-heater"): "HEATER=chamber_heater",
            ("gcode_macro _COOLDOWN_TICK", "ramp-last-step"): "ramp complete",
            ("gcode_macro _COOLDOWN_TICK", "abort-bed-override"): "bed target changed",
            ("gcode_macro _COOLDOWN_TICK", "abort-chamber-override"): "heaters off",
            ("gcode_macro _COOLDOWN_TICK", "chamber-released"): "chamber ceiling released",
            ("gcode_macro _COOLDOWN_TICK", "watchdog"): "watchdog",
            ("gcode_macro _COOLDOWN_TICK", "abort-new-print"): "_COOLDOWN_CLEAR KEEP_POLICY=1",
            ("gcode_macro _COOLDOWN_CLEAR", "clears-policy"): "VARIABLE=policy_set VALUE=0",
            ("gcode_macro _CHAMBER_VENT", "floor-set-runs"): "VARIABLE=vent_active VALUE=1",
            ("gcode_macro _COOLDOWN_TICK", "assist-engage"): "assist engaged",
            ("gcode_macro _COOLDOWN_TICK", "assist-ceiling"): "assist engaged",
            ("gcode_macro _COOLDOWN_TICK", "assist-release"): "assist released",
            # err 10, over 9: the exhaust saturates at 5 C and the curtain
            # takes the remaining 4 of its own 5 C band -> 0.45 * 0.8 = 0.36.
            ("gcode_macro _COOLDOWN_TICK", "assist-curtain"): "SET_FAN_SPEED FAN=fan2 SPEED=0.36",
            ("gcode_macro _COOLDOWN_TICK", "assist-min-fan"): "CHAMBER_ASSIST_FLOOR FLOOR=0.217",
            ("gcode_macro _COOLDOWN_TICK", "assist-curtain-withheld"): "CHAMBER_ASSIST_FLOOR FLOOR=0.217",
            # A bed-only ramp claims a NEUTRAL ceiling -- the chamber's own
            # 30 C -- so the PI asks for nothing and the assist floor alone
            # drives the fan. It used to claim chamber-6 = 24, which saturates.
            ("gcode_macro _COOLDOWN_TICK", "assist-bed-only-claims-ceiling"): "SAVE_VARIABLE VARIABLE=ch_target VALUE=30.0",
            # Paired with the forbid below: this proves the scenario really did
            # schedule a ramp, so "no CHAMBER_ON" means the guard worked rather
            # than that the macro took some other branch entirely.
            ("gcode_macro COOLDOWN_START", "bed-only"): "UPDATE_DELAYED_GCODE ID=COOLDOWN_TIMER",
            ("gcode_macro _COOLDOWN_TICK", "assist-bed-only-releases-ceiling"): "SAVE_VARIABLE VARIABLE=ch_target VALUE=0.0",
            ("gcode_macro END_PRINT", "cooldown-installed"): "COOLDOWN_START",
            ("gcode_macro END_PRINT", "cooldown-absent"): "TURN_OFF_HEATERS",
            ("gcode_macro CANCEL_PRINT", "cooldown-installed"): "COOLDOWN_ABORT",
            ("gcode_macro _CHAMBER_GUARD", "job-complete-no-cooldown"): "CHAMBER_OFF",
            ("gcode_macro _CHAMBER_GUARD", "job-complete-cooling"): "UPDATE_DELAYED_GCODE ID=CHAMBER_GUARD",
            ("gcode_macro _CHAMBER_GUARD", "cooldown-absent"): "CHAMBER_OFF",
            ("gcode_macro CHAMBER_AUTOSTART", "cooldown-installed"): "COOLDOWN_ABORT",
            # M141 S0 is a decision and must be recorded as one; any positive
            # target must clear that record rather than inherit it.
            ("gcode_macro M141", "ramp-owns-it"): "deferred",
            ("gcode_macro M141", "declined"):
                "VARIABLE=control_declined VALUE=1",
            ("gcode_macro M141", "standalone"):
                "VARIABLE=control_declined VALUE=0",
            ("gcode_macro CHAMBER_AUTOSTART", "declined"):
                "chamber control off",
            ("gcode_macro CHAMBER_AUTOSTART", "slicer-target"):
                "as requested by the slicer",
            ("gcode_macro CHAMBER_AUTOSTART", "inferred"): "inferred",
            # The budget branches. A whole-chain 0.05 fits, a single pixel at
            # full fits and must carry its INDEX through, and the whole chain at
            # full white cannot fit and must come back scaled.
            # NOZZLE_LIGHT enforces nothing now: it hands off and then reports.
            ("gcode_macro NOZZLE_LIGHT", "whole-chain"): "LIGHT_STATUS",
            ("gcode_macro NOZZLE_LIGHT", "spotlight"): "INDEX=7",
            ("gcode_macro LIGHT_PATTERN", "counting"): "INDEX=20",
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
            ("gcode_macro SET_LED", "chain"): "INDEX=20",
            ("gcode_macro SET_LED", "index-masked"): "INDEX=20",
            ("gcode_macro SET_LED", "index-clear"): "WHITE=1.0",
            # Renormalization: scaled down, and never silent about it.
            ("gcode_macro SET_LED", "chain-over"): "renormalized",
            ("gcode_macro SET_LED", "index-over"): "renormalized",
        }
        # The inactive tick must produce nothing at all.
        forbid = {("gcode_macro SET_LED", "other-led"): "INDEX=20",
                  # The clamp must bite: axis_maximum is 200 in the fixture, so
                  # a ring at 500 mm would be off the machine entirely.
                  ("gcode_macro CHAMBER_STIR_ORBIT", "clamped"): "X500",
                  ("gcode_macro CHAMBER_STIR_ORBIT", "no-budget"): "G2 ",
                  # On a machine with no load cell, forcing the overlay costs a
                  # heat-up and a contact probe on EVERY print.
                  ("gcode_macro START_PRINT", "eddy-only"): "METHOD=force_overlay",
                  # A matching version must say nothing at all.
                  ("gcode_macro SVZERO_REQUIRE", "version-match"): "profile expects",
                  ("gcode_macro _SVZERO_CHECK_PROFILE", "handshake-seen"): "not sliced by",
                  # Never abort: the whole point is graceful degradation.
                  ("gcode_macro SVZERO_REQUIRE", "version-mismatch"): "action_raise_error",
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
                  ("gcode_macro _COOLDOWN_TICK", "assist-min-fan"): "FLOOR=1.0",
                  ("gcode_macro _COOLDOWN_TICK", "assist-curtain-withheld"): "SET_FAN_SPEED FAN=fan2",
                  # Curtain is off by default, so the default assist scenario
                  # must never reach for fan2.
                  ("gcode_macro _COOLDOWN_TICK", "assist-engage"): "FAN=fan2",
                  # A ramp with no chamber leg must never start the loop against
                  # the zero it carries -- that is a full-speed exhaust draft.
                  ("gcode_macro COOLDOWN_START", "bed-only"): "CHAMBER_ON",
                  # The new-print abort must not erase the policy that print
                  # armed for itself behind the same blocking wait.
                  ("gcode_macro _COOLDOWN_TICK", "abort-new-print"): "VARIABLE=policy_set VALUE=0",
                  ("gcode_macro _COOLDOWN_CLEAR", "keeps-policy"): "VARIABLE=policy_set VALUE=0",
                  # A release must not reach any abort path at all.
                  ("gcode_macro _COOLDOWN_TICK", "chamber-released"): "_COOLDOWN_",
                  # Deferring must not touch ch_target on the way past.
                  ("gcode_macro M141", "ramp-owns-it"): "SAVE_VARIABLE",
                  # A zero floor must never arm the supervisor. vent_active is
                  # what puts the tach guard on a fan that is off on purpose.
                  ("gcode_macro _CHAMBER_VENT", "floor-zero-is-off"): "vent_active VALUE=1",
                  ("gcode_macro _CHAMBER_VENT", "floor-rounds-to-off"): "vent_active VALUE=1",
                  ("gcode_macro SET_LED", "index-masked"): "WHITE=1.0",
                  ("gcode_macro SET_LED", "index-clear"): "INDEX=20",
                  # A renormalized call must not pass the raw value through.
                  ("gcode_macro SET_LED", "chain-over"): "WHITE=1.0",
                  ("gcode_macro SET_LED", "index-over"): "WHITE=1.0",
                  ("gcode_macro SET_LED", "chain"): "renormalized",
                  ("gcode_macro NOZZLE_LIGHT", "whole-chain"): "renormalized",
                  # The independent check on the solver. SET_LED costs the
                  # answer with the same constants and refuses to scale it, so
                  # a solve that overspends says so here without a meter.
                  ("gcode_macro NOZZLE_LIGHT", "max"): "renormalized",
                  ("gcode_macro NOZZLE_LIGHT", "max-starved"): "renormalized",
                  ("gcode_macro LIGHT_PATTERN", "counting"): "refused",
                  ("gcode_macro LIGHT_PATTERN", "over-budget"): "SET_LED",
                  ("gcode_macro _COOLDOWN_TICK", "inactive"): "SET_GCODE_VARIABLE",
                  ("gcode_macro END_PRINT", "cooldown-installed"): "TURN_OFF_HEATERS",
                  ("gcode_macro END_PRINT", "cooldown-absent"): "COOLDOWN_START",
                  ("gcode_macro CANCEL_PRINT", "cooldown-absent"): "COOLDOWN_ABORT",
                  ("gcode_macro CHAMBER_AUTOSTART", "cooldown-absent"): "COOLDOWN_ABORT",
                  # The whole point of the declined branch: the loop must not
                  # start, and no ceiling may be inferred behind the request.
                  ("gcode_macro CHAMBER_AUTOSTART", "declined"): "CHAMBER_ON",
                  ("gcode_macro CHAMBER_AUTOSTART", "inferred"):
                      "as requested by the slicer",
                  ("gcode_macro M141", "declined"):
                      "VARIABLE=control_declined VALUE=0",
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
