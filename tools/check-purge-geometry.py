#!/usr/bin/env python3
"""Render PURGE_LINE for real and measure the G-code it emits.

The ring arithmetic is closed-form algebra that has to agree with a drawing
loop written separately from it. Checking the algebra against itself proves
nothing, so this renders the ACTUAL macro through Klipper's own Jinja
environment and then measures the emitted moves as a plotter would:

  * arc length is summed from the G2/G3 arcs themselves, not from `path`
  * extruded volume is summed from the E words, not from `e_tot`
  * start and end are read off the moves and checked to be antipodal

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


def render(tmpl, varz, noz, obj_poly, vol_max=10.0):
    env = jinja2.Environment("{%", "%}", "{", "}", undefined=jinja2.StrictUndefined)
    env.globals["printer"] = Obj({
        "gcode_macro _PURGE": Obj(varz),
        "gcode_macro _BRUSH": Obj({"retracted": 0.0, "owed_max": 6.0, "retract_f": 2700}),
        "save_variables": Obj({"variables": Obj({"end_retract": 0.0})}),
        "exclude_object": Obj({"objects": [Obj({"polygon": obj_poly})]}) if obj_poly else None,
        "toolhead": Obj({"max_accel": 5000.0}),
    })
    if not obj_poly:
        del env.globals["printer"]["exclude_object"]
    params = Obj({"VOL_MAX": str(vol_max), "FIL_D": "1.75", "NOZZLE": str(noz),
                  "CLEAR": "4.0", "VOLUME": "0", "DRY": "0"})
    return env.from_string(tmpl).render(params=params)


NUM = r"(-?\d+\.?\d*)"


def measure(gcode):
    """Walk the emitted moves. Returns arc length, extrusion, and the ends."""
    x = y = None
    arc = ext = 0.0
    first_xy = last_xy = None
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
        if ge and code in (1, 2, 3) and (gx or gy):
            ext += float(ge.group(1))
        if code in (2, 3) and gi and gj and x is not None:
            cx, cy = x + float(gi.group(1)), y + float(gj.group(1))
            r = math.hypot(x - cx, y - cy)
            a0 = math.atan2(y - cy, x - cx)
            a1 = math.atan2(ny - cy, nx - cx)
            sweep = (a1 - a0) % TAU if code == 3 else (a0 - a1) % TAU
            if abs(nx - x) < 1e-6 and abs(ny - y) < 1e-6:
                sweep = TAU                       # start == end is a FULL circle
            arc += r * sweep
            if first_xy is None:
                first_xy = (x, y)
            last_xy = (nx, ny)
        elif code == 1 and nx is not None and x is not None and ge:
            arc += math.hypot(nx - x, ny - y)     # the radial step-ins extrude too
        if nx is not None:
            x, y = nx, ny
    return arc, ext, first_xy, last_xy


def main():
    cfg = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "purge_line.cfg")
    varz, tmpl = load(cfg)
    poly = [[60.0, 60.0], [95.0, 60.0], [95.0, 95.0], [60.0, 95.0]]
    ox1, oy0 = 95.0, 60.0
    bad = 0
    print("%-6s %4s %8s %9s %9s %8s %8s  %s"
          % ("nozzle", "n", "radius", "want mm3", "got mm3", "arc mm", "180deg", "verdict"))
    for noz in (0.2, 0.4, 0.6, 0.8, 1.0):
        out = render(tmpl, varz, noz, poly)
        info = re.search(r"ring n=(\d+) cx=" + NUM + r" cy=" + NUM
                         + r" r_out=" + NUM + r" r_in=" + NUM, out)
        if not info:
            print("  %.1f  NO RING EMITTED (fell back to the line)" % noz)
            bad += 1
            continue
        n = int(info.group(1)); cx = float(info.group(2)); cy = float(info.group(3))
        r_out = float(info.group(4)); r_in = float(info.group(5))
        arc, ext, s_xy, e_xy = measure(out)
        bead = (varz["bead_w_ratio"] * noz) * (varz["bead_h_ratio"] * noz)
        want = varz["vol_b"] + varz["vol_k"] * noz * noz
        fil_a = math.pi / 4 * 1.75 ** 2
        got = ext * fil_a
        # start and end must be 180 deg apart as seen from the ring centre
        a_s = math.atan2(s_xy[1] - cy, s_xy[0] - cx)
        a_e = math.atan2(e_xy[1] - cy, e_xy[0] - cx)
        sep = math.degrees(abs((a_s - a_e + math.pi) % TAU - math.pi))
        # and the END must be the one nearer the object box
        d_s = math.hypot(max(60.0 - s_xy[0], 0, s_xy[0] - ox1),
                         max(oy0 - s_xy[1], 0, s_xy[1] - 95.0))
        d_e = math.hypot(max(60.0 - e_xy[0], 0, e_xy[0] - ox1),
                         max(oy0 - e_xy[1], 0, e_xy[1] - 95.0))
        ok_vol = abs(got - want) / want < 0.02
        ok_arc = abs(arc * bead - got) / got < 0.02
        ok_sep = abs(sep - 180.0) < 0.5
        ok_dir = d_e < d_s
        ok_r = r_out <= varz["r_max"] + 1e-6 and r_in >= varz["r_min"] - 1e-6
        verdict = "ok" if all((ok_vol, ok_arc, ok_sep, ok_dir, ok_r)) else "FAIL%s%s%s%s%s" % (
            "" if ok_vol else " vol", "" if ok_arc else " arc", "" if ok_sep else " sep",
            "" if ok_dir else " dir", "" if ok_r else " r")
        bad += 0 if verdict == "ok" else 1
        print("%-6s %4d %8.3f %9.2f %9.2f %8.2f %8.1f  %s"
              % (noz, n, r_out, want, got, arc, sep, verdict))
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
    print("end is nearer the object than start, on every nozzle" if not bad
          else "%d PROBLEM(S)" % bad)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
