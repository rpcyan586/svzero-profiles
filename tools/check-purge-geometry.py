#!/usr/bin/env python3
"""Render PURGE_LINE for real and measure the G-code it emits.

The ring arithmetic is closed-form algebra that has to agree with a drawing
loop written separately from it. Checking the algebra against itself proves
nothing, so this renders the ACTUAL macro through Klipper's own Jinja
environment and then measures the emitted moves as a plotter would:

  * arc length is summed from the G2/G3 arcs themselves, not from `path`,
    with each sweep computed the way Klipper's gcode_arcs.py computes it --
    including its exact-zero test for a full circle, which float residue can
    fail, leaving no arc and all of its E in one spot (2026-10-13)
  * extruded volume is summed from the E words, not from `e_tot`
  * start and end are read off the moves and checked to be antipodal
  * every nozzle the pack ships, across a grid of part positions, must lay at
    least one full lap and a half
  * the prime is ROLLED IN along the outer circle and never extruded standing
    still (2026-10-13): no E-only move before the first arc, the roll ends at
    the ring start, carries prime + tip, and is no richer per mm than the ring
    while within roll_halves_max -- checked with and without a debt to repay

Klipper builds its environment as Environment('{%', '%}', '{', '}') with no
extensions (gcode_macro.py:74) -- SINGLE braces for variables -- so the same
delimiters are used here. Run it under klippy-env, whose Jinja is 2.11.3; the
workstation's 3.x is a pre-check, never the gate.

Usage:  ~/klippy-env/bin/python check-purge-geometry.py [purge_line.cfg]
"""
import configparser, math, os, re, sys
import jinja2

TAU = 2 * math.pi


class Obj(dict):
    """Attribute access over a dict, the way Klipper's printer object reads."""
    def __getattr__(self, k):
        try:
            return self[k]
        except KeyError:
            raise AttributeError(k)


def load(path):
    cp = configparser.RawConfigParser(strict=False)
    cp.read(path)
    varz, tmpl = {}, None
    for sec in cp.sections():
        if sec == "gcode_macro _PURGE":
            for k, v in cp.items(sec):
                if k.startswith("variable_"):
                    v = v.split("#")[0].strip()
                    try:
                        varz[k[9:]] = float(v) if "." in v or "e" in v.lower() else int(v)
                    except ValueError:
                        varz[k[9:]] = v
        if sec == "gcode_macro PURGE_LINE":
            tmpl = cp.get(sec, "gcode")
    return varz, tmpl


def render(tmpl, varz, noz, obj_poly, vol_max=10.0, scrub=0.0, owed=0.0):
    env = jinja2.Environment("{%", "%}", "{", "}", undefined=jinja2.StrictUndefined)
    env.globals["printer"] = Obj({
        "gcode_macro _PURGE": Obj(varz),
        "gcode_macro _BRUSH": Obj({"retracted": scrub, "owed_max": 6.0, "retract_f": 2700}),
        "save_variables": Obj({"variables": Obj({"end_retract": owed})}),
        "exclude_object": Obj({"objects": [Obj({"polygon": obj_poly})]}) if obj_poly else None,
        "toolhead": Obj({"max_accel": 5000.0}),
    })
    if not obj_poly:
        del env.globals["printer"]["exclude_object"]
    params = Obj({"VOL_MAX": str(vol_max), "FIL_D": "1.75", "NOZZLE": str(noz),
                  "CLEAR": "4.0", "VOLUME": "0", "DRY": "0"})
    return env.from_string(tmpl).render(params=params)


NUM = r"(-?\d+\.?\d*)"


def roll_split(gcode):
    """The roll's RESPOND line, and whether any E-only move precedes the first arc."""
    roll = re.search(r"roll halves=(\d+) len=" + NUM + r" e=" + NUM + r" e_mm=" + NUM + r" ring_e_mm=" + NUM, gcode)
    still = False
    for line in gcode.splitlines():
        line = line.strip()
        if re.match(r"^G[23]\b", line):
            break
        if re.match(r"^G1\b", line) and re.search(r"[ ]E" + NUM, line) and not re.search(r"[ ][XY]" + NUM, line):
            still = True
    return roll, still


def measure(gcode, skip_arcs=0):
    """Walk the emitted moves. Returns arc length, extrusion, and the ends.
    The first skip_arcs arcs -- the rolled prime -- are walked but not counted."""
    x = y = None
    roll_ext = 0.0
    roll_end = None
    seen_arcs = 0
    arc = ext = 0.0
    first_xy = last_xy = None
    sweeps = []
    for line in gcode.splitlines():
        line = line.strip()
        m = re.match(r"^G([0123])\b", line)
        if not m:
            continue
        code = int(m.group(1))
        gx = re.search(r"[ ]X" + NUM, line)
        gy = re.search(r"[ ]Y" + NUM, line)
        ge = re.search(r"[ ]E" + NUM, line)
        gi = re.search(r"[ ]I" + NUM, line)
        gj = re.search(r"[ ]J" + NUM, line)
        nx = float(gx.group(1)) if gx else x
        ny = float(gy.group(1)) if gy else y
        # Only E that travels DEPOSITS. The stationary E-only moves -- the
        # prime, the `tip` seating advance, the `retract_after` pullback -- are
        # filament bookkeeping, not bead, and counting them made every nozzle
        # read 0.48 mm3 light (0.2 mm of filament: tip 0.2 minus retract 0.4).
        rolling = code in (2, 3) and seen_arcs < skip_arcs
        if ge and code in (1, 2, 3) and (gx or gy):
            if rolling:
                roll_ext += float(ge.group(1))
            else:
                ext += float(ge.group(1))
        if code in (2, 3) and gi and gj and x is not None:
            # gcode_arcs.py planArc, line for line: what Klipper will draw,
            # not what the macro meant.
            r_p, r_q = -float(gi.group(1)), -float(gj.group(1))
            c_p, c_q = x - r_p, y - r_q
            r = math.hypot(r_p, r_q)
            ra, rb = nx - c_p, ny - c_q
            sweep = math.atan2(r_p * rb - r_q * ra, r_p * ra + r_q * rb)
            if sweep < 0.:
                sweep += TAU
            if code == 2:
                sweep -= TAU
            if sweep == 0. and x == nx and y == ny:
                sweep = TAU
            sweep = abs(sweep)
            seen_arcs += 1
            if rolling:
                roll_end = (nx, ny)
                x, y = nx, ny
                continue
            arc += r * sweep
            sweeps.append(sweep)
            if first_xy is None:
                first_xy = (x, y)
            last_xy = (nx, ny)
        elif code == 1 and nx is not None and x is not None and ge:
            arc += math.hypot(nx - x, ny - y)     # the radial step-ins extrude too
        if nx is not None:
            x, y = nx, ny
    return arc, ext, first_xy, last_xy, sweeps, roll_ext, roll_end


def main():
    cfg = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "klipper", "config", "purge_line.cfg")
    varz, tmpl = load(cfg)
    bad = 0
    # Part positions: a 35 mm square at the grid of lower-left corners below,
    # plus the 2026-10-12 backcase, whose ring Klipper collapsed. Where the
    # ring lands decides the float residue, so one position proves nothing.
    corners = [(x0, y0) for x0 in (20.0, 40.0, 57.3, 60.0, 66.979)
               for y0 in (40.0, 58.296, 60.0, 75.0)]
    print("%-6s %4s %6s %8s %8s %9s %9s %6s  %s"
          % ("nozzle", "n", "laps", "r_out", "r_in", "want mm3", "got mm3", "180deg", "verdict"))
    for noz in (0.2, 0.25, 0.3, 0.4, 0.5, 0.6, 0.8, 1.0, 1.2, 1.4):
        worst = None
        fails = set()
        for (x0, y0), (scrub, owed) in [(c, d) for c in corners for d in ((0.0, 0.0), (3.0, 4.0))]:
            ox1, oy1 = x0 + 35.0, y0 + 35.0
            poly = [[x0, y0], [ox1, y0], [ox1, oy1], [x0, oy1]]
            out = render(tmpl, varz, noz, poly, scrub=scrub, owed=owed)
            roll, still = roll_split(out)
            if not roll:
                fails.add("no roll at %s,%s" % (x0, y0))
                continue
            halves = int(roll.group(1))
            info = re.search(r"ring n=(\d+) cx=" + NUM + r" cy=" + NUM + r" r_out=" + NUM
                             + r" r_in=" + NUM + r" step=" + NUM + r" bead=" + NUM + r"x" + NUM
                             + r" vol=" + NUM, out)
            if not info:
                fails.add("no ring at %s,%s" % (x0, y0))
                continue
            n = int(info.group(1)); cx = float(info.group(2)); cy = float(info.group(3))
            r_out = float(info.group(4)); r_in = float(info.group(5))
            bead = float(info.group(7)) * float(info.group(8)); laid = float(info.group(9))
            arc, ext, s_xy, e_xy, sweeps, roll_ext, roll_end = measure(out, skip_arcs=halves)
            owe = scrub + owed + varz["tip"]
            want = varz["vol_b"] + varz["vol_k"] * noz * noz
            got = ext * math.pi / 4 * 1.75 ** 2
            laps = sum(sweeps) / TAU
            a_s = math.atan2(s_xy[1] - cy, s_xy[0] - cx)
            a_e = math.atan2(e_xy[1] - cy, e_xy[0] - cx)
            sep = math.degrees(abs((a_s - a_e + math.pi) % TAU - math.pi))
            d_s = math.hypot(max(x0 - s_xy[0], 0, s_xy[0] - ox1), max(y0 - s_xy[1], 0, s_xy[1] - oy1))
            d_e = math.hypot(max(x0 - e_xy[0], 0, e_xy[0] - ox1), max(y0 - e_xy[1], 0, e_xy[1] - oy1))
            checks = {
                "collapsed arc": min(sweeps) > 0.01,
                "vol": got >= want * 0.98 and abs(got - laid) / laid < 0.02,
                "arc": abs(arc * bead - got) / got < 0.02,
                "laps": laps >= 1.5 - 1e-3,
                "sep": abs(sep - 180.0) < 0.5,
                "dir": d_e < d_s,
                "r": r_out <= varz["r_max"] + 1e-6 and r_in >= varz["r_min"] - 1e-6,
                "stationary prime": not still,
                "roll ends off the start": roll_end is not None and math.hypot(roll_end[0] - s_xy[0], roll_end[1] - s_xy[1]) < 2e-3,
                "roll E": abs(roll_ext - owe) < 0.002 * max(halves, 1) + 1e-3,
                "roll richer than ring": halves >= varz["roll_halves_max"]
                    or float(roll.group(4)) <= float(roll.group(5)) + 1e-4,
            }
            fails.update(k for k, ok in checks.items() if not ok)
            if worst is None or laps < worst[1]:
                worst = (n, laps, r_out, r_in, want, got, sep)
        verdict = "ok" if not fails else "FAIL " + ", ".join(sorted(fails))
        bad += 0 if verdict == "ok" else 1
        if worst:
            print("%-6s %4d %6.2f %8.3f %8.3f %9.2f %9.2f %6.1f  %s" % ((noz,) + worst + (verdict,)))
        else:
            print("%-6s  %s" % (noz, verdict))
    # The ring/line decision turns on `disc`, which the half-lap rewrote. An
    # object that leaves no room for a circle must still purge, as a line.
    print()
    # Sized deliberately: a 12 mm southern margin is too tight for a circle
    # (needs clear + radius, about 17 mm at this nozzle) but ample for a line
    # (needs one bead width plus the edge). An object big enough to block BOTH
    # exercises the documented "then to nothing" path instead, which is a
    # different case and not what this is testing.
    big = [[5.0, 12.0], [148.0, 12.0], [148.0, 148.0], [5.0, 148.0]]
    out = render(tmpl, varz, 0.4, big)
    has_ring = "ring n=" in out
    moves = len([l for l in out.splitlines() if re.match(r"^\s*G[0123]\b", l)])
    extrudes = len([l for l in out.splitlines() if re.search(r"[ ]E" + NUM, l)])
    ok_fb = (not has_ring) and moves > 0 and extrudes > 0
    print("crowded bed: ring=%s, %d moves, %d extruding  %s"
          % (has_ring, moves, extrudes, "ok" if ok_fb else "FAIL -- no purge at all"))
    bad += 0 if ok_fb else 1

    print()
    print("at least a lap and a half, drawn as Klipper draws it, end nearer the part, on every nozzle and position" if not bad
          else "%d PROBLEM(S)" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
