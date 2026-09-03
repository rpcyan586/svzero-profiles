#!/usr/bin/env python3
"""Slice the synthetic test object with every slicer, headless, and measure the
G-code. No GUI, no import dialog, no human.

WHY THIS EXISTS

Two things in this pack were carried for weeks as "verify this before trusting
it", and both are unverifiable by reading files:

  * `PCT_REFERENCE` in generate.py. SuperSlicer expresses most speeds as a
    percentage of another speed, the Orca emitter resolves that chain, and the
    chain itself was taken from documentation. Wrong, it writes plausible
    numbers into every Orca profile and nothing flags it.
  * Which keys a slicer actually accepts. `SS_ONLY` was maintained by hand, one
    key at a time, as each one surfaced in a GUI error dialog.

Both fall out of one slice. The slicer resolves the percentages, so the emitted
feedrates ARE the answer, and every one of the three prints the keys it refused
to load.

WHAT IT MEASURES, per feature type

  speed   from the F word actually in force on each extruding move, not from
          the config -- config says what was asked, G-code says what was emitted
  width   back-computed from the extrusion: w = dE * filament_area / (L * layer)
  flow    width x layer x speed, against the machine and filament ceilings
  accel   peak M204, against the machine limit. A sliced file asking for more
          acceleration than printer.cfg allows RAISES the machine, because
          Klipper's SET_VELOCITY_LIMIT assigns rather than clamps.

NOT a print test. It proves what the slicer emitted, which is a different claim
from the part being good.

    ./slice-harness.py                       # 0.4 nozzle, Standard, all three
    ./slice-harness.py --nozzle 0.6 --tier Draft
    ./slice-harness.py --slicers ss          # just SuperSlicer
    ./slice-harness.py --keep                # leave the G-code for inspection
"""

import argparse
import collections
import glob
import json
import math
import os
import re
import statistics
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
import generate as G  # noqa: E402

# The default work dir is under $HOME on purpose. PrusaSlicer and OrcaSlicer are
# Flatpaks with `filesystems=home` and a PRIVATE /tmp -- an STL under /tmp is
# invisible to them and the slice fails with a file-not-found that reads like a
# path typo. See docs/PIPELINE.md § The Flatpak catch.
WORK = os.path.expanduser("~/.svzero-slicer-sandbox/harness")
PRUSA_ID = "com.prusa3d.PrusaSlicer"
ORCA_ID = "com.orcaslicer.OrcaSlicer"

# Every slicer names the same extrusion something different. Canonical names are
# the pack's own vocabulary, so one table can compare all three.
FEATURE = {
    # PrusaSlicer / SuperSlicer
    "External perimeter": "external perimeter",
    "Perimeter": "perimeter",
    "Overhang perimeter": "overhang perimeter",
    "Internal infill": "infill",
    "Solid infill": "solid infill",
    "Top solid infill": "top solid infill",
    "Bridge infill": "bridge",
    "Internal bridge infill": "internal bridge",
    "Gap fill": "gap fill",
    "Thin wall": "thin wall",
    "Skirt/Brim": "skirt",
    # OrcaSlicer
    "Outer wall": "external perimeter",
    "Inner wall": "perimeter",
    "Overhang wall": "overhang perimeter",
    "Sparse infill": "infill",
    "Internal solid infill": "solid infill",
    "Top surface": "top solid infill",
    "Bottom surface": "solid infill",
    "Bridge": "bridge",
    "Internal Bridge": "internal bridge",
    "Gap infill": "gap fill",
    "Skirt": "skirt",
    "Brim": "skirt",
}
ORDER = ["external perimeter", "perimeter", "overhang perimeter", "infill",
         "solid infill", "top solid infill", "bridge", "internal bridge",
         "gap fill", "thin wall", "skirt"]


# --------------------------------------------------------------------- config

def build_sections():
    """The fully derived section view -- the same one every emitter consumes."""
    model = G.load("model.json")
    ss = G.sections_from_tree(G.load("presets.json"), model)
    G.emit_stock_tier(model, ss, G.load("keymap.json"), G.load("tier-anchors.json"))
    G.apply_speed_model(model, ss)
    G.apply_flow_model(model, ss)
    G.apply_shrinkage(ss, G.load("filament-shrink.json"))
    G.apply_8m_overrides(ss, G.load("filament-8m.json"))
    return model, ss


def preset_names(ss, nozzle, tier):
    want = re.compile(r"^print:SV Zero %sn - [\d.]+mm \(%s\)$" % (re.escape(nozzle), tier))
    hit = next((k for k in ss if want.match(k)), None)
    if not hit:
        raise SystemExit("no print preset for %s nozzle, %s tier" % (nozzle, tier))
    return "printer:SV Zero %sn" % nozzle, hit


def write_ss_config(ss, printer, prnt, filament, path):
    """SuperSlicer takes one flat config on --load, in ITS OWN dialect.

    Deliberately not install-presets.py's output: that resolves widths to
    millimetres and speeds to absolute mm/s for PrusaSlicer, which would hand
    SuperSlicer the answer and measure nothing. The percentages must reach it
    intact -- resolving them is the thing under test.
    """
    cfg = collections.OrderedDict()
    for name in (printer, prnt, filament):
        cfg.update(G.flatten(ss, name))
    with open(path, "w", encoding="utf-8") as fh:
        for k, v in cfg.items():
            fh.write("%s = %s\n" % (k, v))
    return cfg


# ---------------------------------------------------------------------- slice

def run(cmd, log):
    p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    with open(log, "w", encoding="utf-8") as fh:
        fh.write(p.stdout + "\n---- stderr ----\n" + p.stderr)
    return p


REJECT = re.compile(r'key = "([a-z0-9_]+)"|contains the following incorrect keys: ([^\n]+)'
                    r'|Unknown option ([a-z0-9_]+)', re.I)


def rejected_keys(text):
    out = set()
    for m in REJECT.finditer(text):
        if m.group(1):
            out.add(m.group(1))
        for grp in (m.group(2), m.group(3)):
            if grp:
                out |= {k.strip().rstrip(",") for k in grp.split(",") if k.strip()}
    return sorted(out)


def slice_ps(work, nozzle, tier, filament, obj):
    d = os.path.join(ROOT, "bundles", "ps-presets")
    prnt = glob.glob(os.path.join(d, "print", "SV Zero %sn - *(%s).ini" % (nozzle, tier)))
    if not prnt:
        return None, "no flattened print preset; run generate.py --emit ps-presets"
    out = os.path.join(work, "ps.gcode")
    p = run(["flatpak", "run", PRUSA_ID,
             "--datadir", os.path.join(work, "psdata"),
             "--load", os.path.join(d, "printer", "SV Zero %sn.ini" % nozzle),
             "--load", prnt[0],
             "--load", os.path.join(d, "filament", "%s.ini" % filament),
             "--export-gcode", "-o", out, obj],
            os.path.join(work, "ps.log"))
    return (out if os.path.exists(out) else None), p.stdout + p.stderr


def slice_ss(work, cfg_path, obj):
    binary = os.environ.get("SUPERSLICER_BIN") or next(
        iter(sorted(glob.glob(os.path.expanduser("~/opt/superslicer-*/superslicer")))), None)
    if not binary:
        return None, "SuperSlicer not installed; see docs/PIPELINE.md"
    out = os.path.join(work, "ss.gcode")
    p = run([binary, "--datadir", os.path.join(work, "ssdata"),
             "--load", cfg_path, "--export-gcode", "-o", out, obj],
            os.path.join(work, "ss.log"))
    return (out if os.path.exists(out) else None), p.stdout + p.stderr


def slice_orca(work, nozzle, tier, filament, obj):
    d = os.path.join(work, "orcadata")
    subprocess.run([sys.executable, os.path.join(HERE, "install-presets.py"), "orca", d],
                   capture_output=True, text=True)
    v = os.path.join(d, "system", "SVZero")
    proc = glob.glob(os.path.join(v, "process", "*%s @SV Zero %s nozzle.json" % (tier, nozzle)))
    if not proc:
        return None, "no Orca process for %s/%s; run generate.py --emit orca,orca-vendor" % (nozzle, tier)
    for stale in glob.glob(os.path.join(work, "plate_*.gcode")):
        os.remove(stale)
    p = run(["flatpak", "run", ORCA_ID, "--datadir", d,
             "--load-settings", "%s;%s" % (
                 os.path.join(v, "machine", "SV Zero %s nozzle.json" % nozzle), proc[0]),
             "--load-filaments", os.path.join(v, "filament", "%s.json" % filament),
             "--slice", "0", "--outputdir", work, obj],
            os.path.join(work, "orca.log"))
    made = sorted(glob.glob(os.path.join(work, "plate_*.gcode")))
    if made:
        out = os.path.join(work, "orca.gcode")
        os.replace(made[0], out)
        return out, p.stdout + p.stderr
    return None, p.stdout + p.stderr


# -------------------------------------------------------------------- analyse

def analyse(path, filament_diameter=1.75):
    """Walk the G-code and bucket every extruding move by feature type.

    Layer height and intended width come from the slicer's own `;HEIGHT:` and
    `;WIDTH:` comments, which all three emit. Deriving the layer height from Z
    instead looks obvious and is wrong: a Z-hop is also a Z change, so on any
    profile with z-hop enabled the "layer height" becomes the hop and every
    width computed from it is nonsense. That is not hypothetical -- it is what
    the first version of this file reported for Orca, 1.596 mm lines out of a
    0.4 mm nozzle, and the number was absurd enough to notice. A subtler error
    would not have been.
    """
    area = math.pi * (filament_diameter / 2.0) ** 2
    x = y = f = 0.0
    relative = True          # our start G-code sets M83; M82 flips it back
    e_abs = 0.0
    feature = None
    layer_h = 0.0
    declared_w = 0.0
    peak_accel = 0.0
    buckets = collections.defaultdict(
        lambda: {"speed": [], "width": [], "declared": [], "len": 0.0})
    num = re.compile(r"([XYZEF])(-?[\d.]+)")

    for raw in open(path, encoding="utf-8", errors="replace"):
        line = raw.strip()
        if line.startswith(";TYPE:"):
            feature = FEATURE.get(line[6:].strip(), line[6:].strip().lower())
            continue
        if line.startswith(";HEIGHT:"):
            try:
                layer_h = float(line[8:])
            except ValueError:
                pass
            continue
        if line.startswith(";WIDTH:"):
            try:
                declared_w = float(line[7:])
            except ValueError:
                pass
            continue
        if not line or line.startswith(";"):
            continue
        line = line.split(";", 1)[0].strip()
        cmd = line.split(" ", 1)[0]
        if cmd == "M83":
            relative = True; continue
        if cmd == "M82":
            relative = False; continue
        if cmd == "G92":
            if "E" in line:
                e_abs = 0.0
            continue
        if cmd == "M204":
            for m in re.finditer(r"[SPT](\d+(?:\.\d+)?)", line):
                peak_accel = max(peak_accel, float(m.group(1)))
            continue
        if cmd not in ("G0", "G1"):
            continue
        w = dict(num.findall(line))
        nx = float(w.get("X", x)); ny = float(w.get("Y", y))
        if "F" in w:
            f = float(w["F"])
        de = 0.0
        if "E" in w:
            de = float(w["E"]) if relative else float(w["E"]) - e_abs
            if not relative:
                e_abs = float(w["E"])
        dist = math.hypot(nx - x, ny - y)
        x, y = nx, ny
        if de > 0 and dist > 0.05 and feature and feature != "custom" and layer_h > 0:
            b = buckets[feature]
            b["speed"].append(f / 60.0)
            # Extrusions are not rectangles. Slic3r-lineage slicers model a
            # stadium cross-section -- a rectangle with semicircular ends --
            # so area = w*h - h^2*(1 - pi/4). Dividing volume by length and
            # layer height alone under-reports every width by that term, which
            # at 0.4 x 0.2 is 12%: enough to look like a real proportionality
            # error and send someone hunting for one.
            xsec = de * area / dist
            b["width"].append((xsec + layer_h * layer_h * (1 - math.pi / 4)) / layer_h)
            if declared_w:
                b["declared"].append(declared_w)
            b["len"] += dist
    return buckets, peak_accel


def med(v):
    return statistics.median(v) if v else float("nan")


def report(name, buckets, peak_accel, model, expect, rejects):
    """Commanded speed is the MAX, not the mean, and that is not a shortcut.

    Cooling slows whole layers: with `slowdown_below_layer_time` set, a small
    object's quick layers are scaled down to hit the minimum layer time, so the
    median speed of a feature is a mixture of "what the profile asked for" and
    "what cooling allowed". The maximum over all layers is the profile's number,
    as long as one layer somewhere ran unclamped -- which the median column
    makes visible: where the two differ, cooling bound.
    """
    print("\n== %s" % name)
    if rejects:
        print("   REFUSED %d key(s): %s" % (len(rejects), ", ".join(rejects)))
    print("   %-20s %6s %9s %9s %8s %8s %9s"
          % ("feature", "moves", "cmd mm/s", "typ mm/s", "width", "decl", "mm3/s"))
    for feat in ORDER:
        if feat not in buckets:
            continue
        b = buckets[feat]
        s_cmd, s_typ = max(b["speed"]), med(b["speed"])
        wd, dc = med(b["width"]), med(b["declared"])
        print("   %-20s %6d %9.1f %9.1f %8.3f %8.3f %9.1f"
              % (feat, len(b["speed"]), s_cmd, s_typ, wd, dc, s_cmd * wd * med(
                  [expect["layer_height"]])))
    cap = model.get("machine", {}).get("limits", {}).get("max_accel")
    if peak_accel:
        flag = "  ** over machine max_accel %g **" % cap if cap and peak_accel > cap else ""
        print("   peak commanded acceleration: %g mm/s2%s" % (peak_accel, flag))
    return {f: (max(b["speed"]), med(b["width"])) for f, b in buckets.items()}


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nozzle", default="0.4")
    ap.add_argument("--tier", default="Standard")
    ap.add_argument("--filament", default="SV Zero PLA - Brass")
    ap.add_argument("--slicers", default="ps,ss,orca")
    ap.add_argument("--workdir", default=WORK)
    ap.add_argument("--keep", action="store_true", help="keep the G-code and configs")
    args = ap.parse_args()

    work = os.path.expanduser(args.workdir)
    os.makedirs(work, exist_ok=True)
    obj = os.path.join(work, "svzero-featurecube.stl")
    if not os.path.exists(obj):
        subprocess.run([sys.executable, os.path.join(HERE, "make-test-object.py"), work],
                       check=True)

    model, ss = build_sections()
    printer, prnt = preset_names(ss, args.nozzle, args.tier)
    flat = G.flatten(ss, prnt)
    expect = {"layer_height": float(flat["layer_height"]),
              "default_speed": float(flat["default_speed"])}
    fil = G.flatten(ss, "filament:%s" % args.filament)
    print("object   %s" % os.path.basename(obj))
    print("preset   %s   +   %s" % (prnt.split(":", 1)[1], args.filament))
    print("model    layer %.2f mm, default_speed %.1f mm/s, nozzle %s"
          % (expect["layer_height"], expect["default_speed"], args.nozzle))

    want = [s.strip() for s in args.slicers.split(",") if s.strip()]
    results = {}
    if "ss" in want:
        cfg = os.path.join(work, "ss-config.ini")
        write_ss_config(ss, printer, prnt, "filament:%s" % args.filament, cfg)
        path, out = slice_ss(work, cfg, obj)
        if path:
            b, acc = analyse(path, float(fil.get("filament_diameter", 1.75)))
            results["SuperSlicer"] = report("SuperSlicer %s" % args.tier, b, acc,
                                            model, expect, rejected_keys(out))
        else:
            print("\n== SuperSlicer: no G-code -- %s" % out.strip().splitlines()[-1:])
    if "ps" in want:
        path, out = slice_ps(work, args.nozzle, args.tier, args.filament, obj)
        if path:
            b, acc = analyse(path, float(fil.get("filament_diameter", 1.75)))
            results["PrusaSlicer"] = report("PrusaSlicer %s (flattened presets)" % args.tier,
                                            b, acc, model, expect, rejected_keys(out))
        else:
            print("\n== PrusaSlicer: no G-code -- %s" % out)
    if "orca" in want:
        path, out = slice_orca(work, args.nozzle, args.tier, args.filament, obj)
        if path:
            b, acc = analyse(path, float(fil.get("filament_diameter", 1.75)))
            results["OrcaSlicer"] = report("OrcaSlicer %s (vendor bundle)" % args.tier,
                                           b, acc, model, expect, rejected_keys(out))
        else:
            print("\n== OrcaSlicer: no G-code -- %s" % out)

    # ---- the thing this was built for: does the master's percentage chain
    # resolve the way generate.py assumes, in the slicer that defines it?
    if "SuperSlicer" in results:
        print("\n== PCT_REFERENCE, measured against SuperSlicer")
        base = expect["default_speed"]
        raw = G.flatten(ss, prnt)
        checked = 0
        for key, ref in G.PCT_REFERENCE.items():
            v = str(raw.get(key, "")).strip()
            feat = {"perimeter_speed": "perimeter",
                    "external_perimeter_speed": "external perimeter",
                    "infill_speed": "infill",
                    "solid_infill_speed": "solid infill",
                    "top_solid_infill_speed": "top solid infill",
                    "bridge_speed": "bridge"}.get(key)
            if not v.endswith("%") or not feat or feat not in results["SuperSlicer"]:
                continue
            resolved = G.resolve_speeds(raw).get(key)
            got = results["SuperSlicer"][feat][0]
            ok = "ok" if abs(float(resolved) - got) <= max(1.0, 0.02 * got) else "MISMATCH"
            print("   %-26s %8s of %-24s predicted %7.1f   emitted %7.1f   %s"
                  % (key, v, ref, float(resolved), got, ok))
            checked += 1
        if not checked:
            print("   nothing to check: no percentage speeds in this preset")
        print("   (base default_speed = %.1f)" % base)

    if not args.keep:
        for f in ("ps.gcode", "ss.gcode", "orca.gcode"):
            p = os.path.join(work, f)
            if os.path.exists(p):
                os.remove(p)


if __name__ == "__main__":
    main()
