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

EXIT CODE IS THE POINT, as of 2026-09-06. A refused key or a slicer that
produced no G-code exits 1. This printed its refusals for weeks and returned 0,
so a green run meant nothing and nobody had a reason to read the detail --
`_why_supports` and the Orca chamber keys were in that output the whole time
while shipping in the published bundles.

IT SLICES THE ARTEFACTS, not only the master. The SuperSlicer arm runs twice:
once against the master, which is what proves the percentage chain resolves the
way generate.py assumes, and once against bundles/ss-presets, which is what
install-presets.py actually writes into a datadir. A defect introduced by an
EMITTER is invisible to a pass that never reads the emitter's output, and that
gap is exactly how three broken artefacts shipped.

Master-pass refusals are reported but do not fail when the key is not a
SuperSlicer option at all -- annotations and Orca-authored keys are meant to be
there and are filtered on emit. Artefact passes are strict.
"""

import argparse
import collections
import glob
import json
import math
import os
import re
import statistics
import struct
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
# Cross-slicer bounds are LOOSE on purpose: measured spreads on identical
# settings run to 1.93x on filament and 2.76x on time. Anything tight enough to
# be interesting here would be a false alarm. These catch gross failure only --
# one slicer ignoring infill or supports entirely.
CROSS_MAX = 4.0
# Extruded volume over the object's solid volume. Wide because it spans 10% and
# 25% infill, 2 to 6 walls, five nozzles, and support material that adds
# material outside the object entirely. Measured 0.27-1.77 across this matrix.
VOL_MIN, VOL_MAX = 0.10, 3.0
# Against a slicer's OWN recorded number, where determinism makes it meaningful.
BASELINE_TOL = 0.02
PRUSA_BRANCH = "stable"      # 2.9.6; the beta alongside it is 3.0
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


def merge_ini(paths, out_path):
    """Concatenate several preset .ini files into one flat config.

    PrusaSlicer's `--load` takes AT MOST ONE file -- passing three fails with
    "At Most 1 required but received 3", which is exactly what this arm did
    from the day it was written. It never produced a G-code, so PrusaSlicer was
    the one slicer never actually exercised, and it is the one whose published
    bundle carried 17 broken multi-line values.
    """
    seen = collections.OrderedDict()
    for path in paths:
        if not os.path.exists(path):
            return None
        for line in open(path, encoding="utf-8"):
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                seen[k.strip()] = v.strip()
    with open(out_path, "w", encoding="utf-8") as fh:
        for k, v in seen.items():
            fh.write("%s = %s\n" % (k, v))
    return out_path


def slice_ps(work, nozzle, tier, filament, obj):
    d = os.path.join(ROOT, "bundles", "ps-presets")
    prnt = glob.glob(os.path.join(d, "print", "SV Zero %sn - *(%s).ini" % (nozzle, tier)))
    if not prnt:
        return None, "no flattened print preset; run generate.py --emit ps-presets"
    cfg = merge_ini([os.path.join(d, "printer", "SV Zero %sn.ini" % nozzle),
                     prnt[0],
                     os.path.join(d, "filament", "%s.ini" % filament)],
                    os.path.join(work, "ps-config.ini"))
    if not cfg:
        return None, "a ps-presets file is missing; run generate.py --emit ps-presets"
    out = os.path.join(work, "ps.gcode")
    # --branch matters now that 3.0 beta is installed beside 2.9.6; without it
    # flatpak picks the beta and this measures the wrong slicer.
    p = run(["flatpak", "run", "--branch=" + PRUSA_BRANCH, PRUSA_ID,
             "--datadir", os.path.join(work, "psdata"),
             "--load", cfg,
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


def totals(path):
    """(filament_mm, filament_cm3, seconds) from a G-code footer.

    All three slicers write the same two comments, which is what makes a
    cross-check possible at all:

        ; filament used [mm] = 687.63
        ; filament used [cm3] = 1.65
        ; estimated printing time (normal mode) = 5m 11s
    """
    mm = cm3 = secs = None
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.startswith(";"):
                continue
            m = re.match(r";\s*filament used \[mm\]\s*=\s*([\d.]+)", line)
            if m:
                mm = float(m.group(1)); continue
            m = re.match(r";\s*filament used \[cm3\]\s*=\s*([\d.]+)", line)
            if m:
                cm3 = float(m.group(1)); continue
            m = re.match(r";\s*estimated printing time \(normal mode\)\s*=\s*(.+)", line)
            if m:
                t, total = m.group(1).strip(), 0.0
                for num, unit in re.findall(r"([\d.]+)\s*([dhms])", t):
                    total += float(num) * {"d": 86400, "h": 3600, "m": 60, "s": 1}[unit]
                secs = total or None
    return mm, cm3, secs


def stl_volume(path):
    """Signed volume of a binary STL, mm3. The absolute anchor.

    Cross-slicer agreement cannot catch an error the three SHARE -- a wrong flow
    ratio, a wrong extrusion width, a nozzle mismatch -- because all three would
    move together and still agree with each other. The object's own solid volume
    is independent of every slicer, and the extruded volume has to bear a
    sensible ratio to it.
    """
    with open(path, "rb") as fh:
        raw = fh.read()
    n = struct.unpack("<I", raw[80:84])[0]
    vol = 0.0
    for i in range(n):
        off = 84 + i * 50
        v = struct.unpack("<12f", raw[off:off + 48])
        a, b, c = v[3:6], v[6:9], v[9:12]
        vol += (a[0] * (b[1] * c[2] - b[2] * c[1])
                - a[1] * (b[0] * c[2] - b[2] * c[0])
                + a[2] * (b[0] * c[1] - b[1] * c[0])) / 6.0
    return abs(vol)


def spread(values):
    """max/min of the non-null values, or None when fewer than two."""
    v = [x for x in values if x]
    return (max(v) / min(v)) if len(v) > 1 else None


def covering_plan(model, filaments, objects):
    """One row per filament, cycling nozzles and tiers alongside.

    NOT the cross-product -- 5 nozzles x 5 tiers x 18 filaments is 450 slices
    per slicer and most would re-prove the same thing. This is a covering plan:
    every nozzle, every tier and every filament appears at least once, in
    max(5, 5, 18) = 18 rows. Filaments dominate, so they set the count and the
    other two ride along.

    Tier gaps are honoured -- 0.2 has no Draft and no Strength -- so a row that
    would land there advances to the next legal tier. That skewing can leave a
    tier uncovered, which is why coverage is REPAIRED and then asserted rather
    than assumed to fall out of the arithmetic.
    """
    nozzles = list(model["machine"]["nozzles"])
    tiers = list(model["tiers"]["_order"]) + list(model["tiers"].get("_variants", []))
    gaps = {k: set(v) for k, v in model["tier_gaps"].items() if not k.startswith("_")}

    def legal(noz, tier):
        return tier not in gaps.get(noz, set())

    # Cycling both factors on i alone CORRELATES them: with 5 nozzles and 5
    # tiers, 0.2 would always be Fine and 1.0 always Strength, so 5 of the 23
    # legal pairs get tested and the same 5 every run. Pick instead the legal
    # tier this nozzle has seen least, breaking ties on the globally least used
    # tier -- 18 rows then cover 18 distinct pairs instead of 5.
    rows = []
    seen_pair = collections.Counter()
    seen_tier = collections.Counter()
    n = max(len(nozzles), len(tiers), len(filaments), len(objects))
    for i in range(n):
        noz = nozzles[i % len(nozzles)]
        tier = min((t for t in tiers if legal(noz, t)),
                   key=lambda t: (seen_pair[(noz, t)], seen_tier[t], tiers.index(t)))
        seen_pair[(noz, tier)] += 1
        seen_tier[tier] += 1
        rows.append((noz, tier, filaments[i % len(filaments)],
                     objects[i % len(objects)]))

    for tier in tiers:
        if not any(r[1] == tier for r in rows):
            noz = next(x for x in nozzles if legal(x, tier))
            rows.append((noz, tier, filaments[len(rows) % len(filaments)],
                         objects[len(rows) % len(objects)]))
    for noz in nozzles:
        if not any(r[0] == noz for r in rows):
            tier = next(t for t in tiers if legal(noz, t))
            rows.append((noz, tier, filaments[len(rows) % len(filaments)],
                         objects[len(rows) % len(objects)]))
    for fil in filaments:
        if not any(r[2] == fil for r in rows):
            rows.append((rows[0][0], rows[0][1], fil,
                         objects[len(rows) % len(objects)]))
    for ob in objects:
        if not any(r[3] == ob for r in rows):
            rows.append((rows[0][0], rows[0][1],
                         filaments[len(rows) % len(filaments)], ob))

    missing = ([n for n in nozzles if not any(r[0] == n for r in rows)]
               + [t for t in tiers if not any(r[1] == t for r in rows)]
               + [f for f in filaments if not any(r[2] == f for r in rows)]
               + [o for o in objects if not any(r[3] == o for r in rows)])
    assert not missing, "covering plan misses %s" % missing
    return rows, nozzles, tiers


def run_matrix(args, model, work, obj, want):
    """Every nozzle, tier, filament and test object at least once, per slicer."""
    filaments = sorted(
        os.path.splitext(f)[0]
        for f in os.listdir(os.path.join(ROOT, "bundles", "ss-presets", "filament"))
        if f.endswith(".ini"))
    objects = [f for f in ("svzero-featurecube.stl", "svzero-overhang.stl",
                           "svzero-tower.stl", "svzero-smallpins.stl")
               if os.path.exists(os.path.join(work, f))]
    rows, nozzles, tiers = covering_plan(model, filaments, objects)
    print("covering plan: %d row(s) -- %d nozzle(s), %d tier(s), %d filament(s), %d object(s)"
          % (len(rows), len(nozzles), len(tiers), len(filaments), len(objects)))
    print("%-3s %-6s %-9s %-24s %-12s %s"
          % ("#", "nozzle", "tier", "filament", "object",
             "  ".join("%-8s" % w for w in want)))
    fails = []
    meas = {}
    d_ss = os.path.join(ROOT, "bundles", "ss-presets")
    for i, (noz, tier, fil, ob) in enumerate(rows, 1):
        obj = os.path.join(work, ob)
        cells = []
        for w in want:
            if w == "ss":
                art = glob.glob(os.path.join(d_ss, "print",
                                             "SV Zero %sn - *(%s).ini" % (noz, tier)))
                cfg = merge_ini([os.path.join(d_ss, "printer", "SV Zero %sn.ini" % noz),
                                 art[0], os.path.join(d_ss, "filament", "%s.ini" % fil)],
                                os.path.join(work, "m-ss.ini")) if art else None
                path, out = slice_ss(work, cfg, obj) if cfg else (None, "no preset for this pair")
            elif w == "ps":
                path, out = slice_ps(work, noz, tier, fil, obj)
            else:
                path, out = slice_orca(work, noz, tier, fil, obj)
            if path:
                mm, cm3, secs = totals(path)
                meas.setdefault(i, {})[w] = (mm, cm3, secs)
            rej = rejected_keys(out) if path else []
            if not path:
                cells.append("NOGCODE")
                fails.append((i, "%sn %s" % (noz, ob.replace("svzero-", "").replace(".stl", "")), tier, fil, w,
                              (out or "").strip().splitlines()[-1:] or ["?"]))
            elif rej:
                cells.append("REFUSED")
                fails.append((i, "%sn %s" % (noz, ob.replace("svzero-", "").replace(".stl", "")), tier, fil, w, [", ".join(rej)]))
            else:
                cells.append("ok")
            if path and os.path.exists(path) and not args.keep:
                os.remove(path)
        print("%-3d %-6s %-9s %-24s %-12s %s"
              % (i, noz, tier, fil[:24], ob.replace("svzero-", "").replace(".stl", ""),
                 "  ".join("%-8s" % c for c in cells)))
    # ---- SANITY ON THE TOTALS
    #
    # Three checks, and they are deliberately not equally strict, because the
    # three slicers genuinely disagree. Measured across this matrix: filament
    # totals spread up to 1.93x and time estimates up to 2.76x on IDENTICAL
    # settings, driven mostly by support generation and infill implementation.
    # That is the "algorithmic variability" and no useful gate can be built
    # across it -- a cross-slicer bound loose enough to accept 1.93x catches
    # almost nothing.
    #
    # So the tight gate is a BASELINE. Each slicer is deterministic: same input,
    # same output. Comparing a slicer against ITS OWN recorded number makes the
    # algorithmic difference irrelevant, and then a 2% band is meaningful and a
    # profile change that quietly alters material or time shows up immediately.
    #   --record  rewrites source/slice-baseline.json.
    base_path = os.path.join(ROOT, "source", "slice-baseline.json")
    base = {}
    if os.path.exists(base_path):
        base = json.load(open(base_path, encoding="utf-8")).get("rows", {})
    fresh = {}
    print("\n== totals")
    print("%-3s %-11s %-8s %-9s %-9s %-9s %-7s"
          % ("#", "object", "slicer", "mm", "cm3", "time s", "vs base"))
    xs_f, xs_t, ratios = [], [], []
    for i, (noz, tier, fil, ob) in enumerate(rows, 1):
        got = meas.get(i, {})
        if not got:
            continue
        sv = stl_volume(os.path.join(work, ob))
        for w in want:
            mm, cm3, secs = got.get(w, (None, None, None))
            if mm is None:
                continue
            key = "%s|%sn|%s|%s|%s" % (w, noz, tier, fil, ob)
            fresh[key] = {"mm": round(mm, 2), "cm3": cm3, "s": secs}
            drift = ""
            if key in base and base[key].get("mm"):
                d = mm / base[key]["mm"]
                drift = "%+.1f%%" % ((d - 1) * 100)
                if abs(d - 1) > BASELINE_TOL:
                    fails.append((i, "%sn %s" % (noz, ob.replace("svzero-", "").replace(".stl", "")), tier, fil, w,
                                  ["filament %+.1f%% vs baseline (%.1f -> %.1f mm)"
                                   % ((d - 1) * 100, base[key]["mm"], mm)]))
            elif base:
                drift = "new"
            print("%-3d %-11s %-8s %-9.1f %-9.3f %-9.0f %-7s"
                  % (i, ob.replace("svzero-", "").replace(".stl", ""), w,
                     mm, cm3 or 0, secs or 0, drift or "-"))
        mms = [v[0] for v in got.values() if v[0]]
        cms = [v[1] for v in got.values() if v[1]]
        secs_all = [v[2] for v in got.values() if v[2]]
        f_sp, t_sp = spread(mms), spread(secs_all)
        if f_sp: xs_f.append(f_sp)
        if t_sp: xs_t.append(t_sp)
        # The absolute anchor. Cross-slicer agreement cannot catch an error the
        # three SHARE -- a wrong flow ratio or extrusion width moves all of them
        # together and they still agree. The object's own solid volume does not
        # come from a slicer.
        for cm3 in cms:
            r = cm3 * 1000.0 / sv
            ratios.append(r)
            if not (VOL_MIN <= r <= VOL_MAX):
                fails.append((i, "%sn %s" % (noz, ob.replace("svzero-", "").replace(".stl", "")), tier, fil, "volume",
                              ["extruded/solid %.2f outside %.2f-%.2f"
                               % (r, VOL_MIN, VOL_MAX)]))
        if f_sp and f_sp > CROSS_MAX:
            fails.append((i, "%sn %s" % (noz, ob.replace("svzero-", "").replace(".stl", "")), tier, fil, "cross",
                          ["filament totals differ %.2fx across slicers" % f_sp]))
    if xs_f:
        print("\n   cross-slicer spread  filament %.2f-%.2fx   time %.2f-%.2fx"
              % (min(xs_f), max(xs_f), min(xs_t or [1]), max(xs_t or [1])))
        print("   extruded/solid       %.2f-%.2f" % (min(ratios), max(ratios)))
    if args.record:
        json.dump({"_what": "Per-slicer totals from a known-good matrix run. The "
                            "tight gate: each slicer is deterministic, so drift "
                            "against its OWN number is meaningful where "
                            "cross-slicer comparison is not.",
                   "_tolerance": BASELINE_TOL,
                   "_regenerate": "./tools/slice-harness.py --matrix --record",
                   "rows": fresh},
                  open(base_path, "w", encoding="utf-8"), indent=2, sort_keys=True)
        open(base_path, "a", encoding="utf-8").write("\n")
        print("   recorded %d baseline entries" % len(fresh))

    print("\ncoverage: nozzles %s | tiers %s | filaments %d of %d | objects %s"
          % (",".join(sorted({r[0] for r in rows})),
             ",".join(sorted({r[1] for r in rows})),
             len({r[2] for r in rows}), len(filaments),
             ",".join(sorted(o.replace("svzero-", "").replace(".stl", "")
                             for o in {r[3] for r in rows}))))
    print("\n== verdict")
    for i, noz, tier, fil, w, why in fails:
        print("   FAIL  row %d  %s  %s %s / %s -- %s" % (i, w, noz, tier, fil, why[0][:90]))
    if not fails:
        print("   %d row(s) x %d slicer(s) = %d slice(s): all produced G-code, none refused a key"
              % (len(rows), len(want), len(rows) * len(want)))
    return 1 if fails else 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--nozzle", default="0.4")
    ap.add_argument("--tier", default="Standard")
    ap.add_argument("--filament", default="SV Zero PLA - Brass")
    ap.add_argument("--slicers", default="ps,ss,orca")
    ap.add_argument("--workdir", default=WORK)
    ap.add_argument("--keep", action="store_true", help="keep the G-code and configs")
    ap.add_argument("--record", action="store_true",
                    help="rewrite source/slice-baseline.json from this run")
    ap.add_argument("--matrix", action="store_true",
                    help="cover every nozzle, tier and filament at least once")
    args = ap.parse_args()

    work = os.path.expanduser(args.workdir)
    os.makedirs(work, exist_ok=True)
    obj = os.path.join(work, "svzero-featurecube.stl")
    subprocess.run([sys.executable, os.path.join(HERE, "make-test-object.py"),
                    work, "--shape", "all"], check=True, capture_output=True)

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
    if args.matrix:
        return run_matrix(args, model, work, obj, want)
    results = {}
    refused = {}
    if "ss" in want:
        # TWO SuperSlicer passes, and the second is the one that matters for
        # shipping. The first slices the MASTER, which is what proves the
        # percentage chain resolves as generate.py assumes. The second slices
        # bundles/ss-presets -- the files install-presets.py actually writes
        # into a datadir -- because a defect introduced by an EMITTER is
        # invisible to a pass that never reads the emitter's output. That gap
        # is why three broken artefacts shipped on 2026-09-06.
        d = os.path.join(ROOT, "bundles", "ss-presets")
        art = glob.glob(os.path.join(d, "print", "SV Zero %sn - *(%s).ini"
                                     % (args.nozzle, args.tier)))
        if art:
            merged = merge_ini([os.path.join(d, "printer", "SV Zero %sn.ini" % args.nozzle),
                                art[0],
                                os.path.join(d, "filament", "%s.ini" % args.filament)],
                               os.path.join(work, "ss-artefact.ini"))
            if merged:
                apath, aout = slice_ss(work, merged, obj)
                arej = rejected_keys(aout)
                refused["SuperSlicer (emitted presets)"] = arej
                print("\n== SuperSlicer %s, EMITTED presets -- %s"
                      % (args.tier, "sliced clean" if apath and not arej
                         else "PROBLEM"))
                if arej:
                    print("   REFUSED %d key(s): %s" % (len(arej), ", ".join(arej)))
                if not apath:
                    print("   no G-code: %s" % (aout.strip().splitlines() or ["?"])[-1])
                    refused["SuperSlicer (emitted presets)"] = arej + ["<no G-code>"]
        cfg = os.path.join(work, "ss-config.ini")
        write_ss_config(ss, printer, prnt, "filament:%s" % args.filament, cfg)
        path, out = slice_ss(work, cfg, obj)
        if path:
            b, acc = analyse(path, float(fil.get("filament_diameter", 1.75)))
            refused["SuperSlicer (master)"] = rejected_keys(out)
            results["SuperSlicer"] = report("SuperSlicer %s" % args.tier, b, acc,
                                            model, expect, rejected_keys(out))
        else:
            print("\n== SuperSlicer: no G-code -- %s" % out.strip().splitlines()[-1:])
    if "ps" in want:
        path, out = slice_ps(work, args.nozzle, args.tier, args.filament, obj)
        if path:
            b, acc = analyse(path, float(fil.get("filament_diameter", 1.75)))
            refused["PrusaSlicer"] = rejected_keys(out)
            results["PrusaSlicer"] = report("PrusaSlicer %s (flattened presets)" % args.tier,
                                            b, acc, model, expect, rejected_keys(out))
        else:
            print("\n== PrusaSlicer: no G-code -- %s" % out)
    if "orca" in want:
        path, out = slice_orca(work, args.nozzle, args.tier, args.filament, obj)
        if path:
            b, acc = analyse(path, float(fil.get("filament_diameter", 1.75)))
            refused["OrcaSlicer"] = rejected_keys(out)
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

    # A REFUSED KEY IS A FAILURE, not a note. This printed the refusals for
    # weeks and returned 0, so nothing downstream could act on it and nobody
    # reading a green run had any reason to look. _why_supports and the Orca
    # chamber keys were listed in that output the whole time.
    # The MASTER pass is expected to carry keys SuperSlicer does not define:
    # authored annotations, and options written for Orca. That is the whole
    # reason the emitters filter. So a master-pass refusal only counts when the
    # key IS a SuperSlicer option and was refused anyway -- that would be a real
    # defect. Artefact passes are strict: nothing there may be refused, because
    # an artefact is what somebody installs.
    ss_known = set()
    try:
        _s = G.load("ss-known-options.json")
        ss_known = set(_s["options"]) | set(_s["metadata"])
    except Exception:
        pass
    expected_in_master = lambda k: k.startswith("_") or (ss_known and k not in ss_known)
    for slicer in list(refused):
        if "master" in slicer and ss_known:
            noted = [k for k in refused[slicer] if expected_in_master(k)]
            if noted:
                print("\n== master pass: %d key(s) not SuperSlicer's, filtered on emit"
                      % len(noted))
                print("   %s" % ", ".join(noted))
            refused[slicer] = [k for k in refused[slicer] if not expected_in_master(k)]

    bad = {k: v for k, v in refused.items() if v}
    missing = [k for k in ("ss", "ps", "orca") if k in want] and [
        n for n in ("SuperSlicer", "PrusaSlicer", "OrcaSlicer")
        if n.lower()[:2] in [w[:2] for w in want] and n not in results]
    print("\n== verdict")
    for slicer, keys in sorted(bad.items()):
        print("   FAIL  %s refused: %s" % (slicer, ", ".join(keys)))
    for n in missing:
        print("   FAIL  %s produced no G-code" % n)
    if not bad and not missing:
        print("   all requested slicers produced G-code and refused nothing")
    return 1 if (bad or missing) else 0


if __name__ == "__main__":
    sys.exit(main())
