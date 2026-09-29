#!/usr/bin/env python3
"""Generate SuperSlicer, PrusaSlicer and OrcaSlicer profiles from one model.

The SuperSlicer and PrusaSlicer bundles in this pack were maintained by hand and
drifted apart: SuperSlicer grew the four-tier nozzle-independent architecture
while PrusaSlicer kept 0.4mm-named tier bases, and 19 of SuperSlicer's presets
ended up inheriting from sections that did not exist. Hand-maintaining N copies
of the same design is what produced that. This generates them instead.

    ./generate.py --check          # report drift, write nothing (CI-safe)
    ./generate.py --emit ss,ps     # write bundles
    ./generate.py --emit orca      # write Orca profile JSONs

WHERE THE MASTER LIVES

source/presets.json. It holds the authored values as a tree of fragments in the
shape PrusaSlicer 3.0 defines in source/ps3/preset-schema.json --
{id, name, condition, features, values, variants, inherits} -- where every
root-to-named-leaf path is one preset and values merge down the path. No
slicer's own file format is the source; SuperSlicer, PrusaSlicer and Orca are
all emitted from it, and every artefact gets the model derivations whether or
not the others are emitted in the same run.

It was a SuperSlicer .ini until 2026-09-01, chosen because SS and PS 2.9 share
all 90 print keys used here while Orca shares 13, so the expensive translation
sat on one edge. That reasoning was about the key space and it still holds --
but a master that is also a shipped artefact leaks its own dialect into
everything downstream, and SuperSlicer has not shipped a stable release since
before 2.7.61 (last tag 2.7.62.0-beta2, 2025-11-19, a prerelease) and had never
once been installed on this workstation, so the bundle written in its format had
never been round-tripped through it. PrusaSlicer 3.0 settled it: its profiles
are a conditional variant tree, which an .ini cannot express, so the master had
to stop being one. See docs/PIPELINE.md.

WHAT IS NOT GENERATED YET

Tier *values*. This emits structure — the preset tree, inheritance, per-nozzle
overrides, layer heights, SQV passing — from the model, and carries existing
values through. Deriving Normal and Quality from the anchors is the next step and
wants the filament-assumption work first, since a tier that advertises a speed
its filament cannot sustain is decoration. See docs/STATUS.md.
"""

import argparse
import collections
import shutil
import json
import math
import os

import re
import sys

# The Orca start G-code lives in start_gcode.py so generate.py and
# install-presets.py cannot drift apart again. Tests load these scripts by path
# with importlib, which does not put this directory on sys.path, so add it.
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from start_gcode import (  # noqa: E402
    ORCA_START_PREAMBLE,
    ORCA_START_CUT,
    ORCA_PURGE_START,
    ORCA_PURGE_END,
)

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SOVOL_VENDOR = os.path.join(ROOT, "source", "vendor-profiles")


def load(name):
    return json.load(open(os.path.join(ROOT, "source", name), encoding="utf-8"))


def computed_motion(value):
    """Tenths for derived speeds/accelerations; keep .0 to mark computation.

    Call after evaluating a reference chain, never on intermediate operands.
    Geometry, material ratios, calibration values and authored settings retain
    their own precision and are deliberately outside this formatter.
    """
    return "%.1f" % value


def parse_ini(path):
    """Bundle -> {section: OrderedDict(key->value)}, preserving order."""
    secs, cur = collections.OrderedDict(), None
    for line in open(path, encoding="utf-8"):
        line = line.rstrip("\n")
        m = re.match(r"^\[([^\]]+)\]\s*$", line)
        if m:
            cur = m.group(1)
            secs[cur] = collections.OrderedDict()
            continue
        if cur is not None and "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            secs[cur][k.strip()] = v.strip()
    return secs


ANNOTATION_PREFIX = "_"     # authored commentary, never a slicer key


def ini_value(v):
    """Render a value for a PrusaSlicer-family .ini.

    Multi-line G-code is stored as a BACKSLASH-N ESCAPE, not a real newline. An
    .ini is line-oriented, so a raw newline splits the value and the remainder is
    parsed as another `key = value` pair. Not theoretical: `start_filament_gcode`
    is authored in source/presets.json with a real newline, so every artefact
    shipped

        start_filament_gcode = ; Material-specific Z offset: ...
        SET_GCODE_OFFSET Z_ADJUST=0.000 MOVE=1

    and SuperSlicer duly reported `SET_GCODE_OFFSET Z_ADJUST` as an unknown
    setting with value `0.000 MOVE=1`. The material Z offset never ran, and
    neither did the pressure-advance line appended after it. Shipped in the
    published bundles until a GUI load said so.

    Escaping here rather than fixing the master fixes the CLASS: two emitters
    also join G-code fragments with a real newline, and both are covered.
    """
    t = str(v)
    return t.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "\\n")


def ini_writable(kv, reject=None):
    """Drop annotation keys, and any key the target slicer is known to refuse.

    A DENY-list, deliberately. The allow-list this replaced was built from
    `superslicer --help-fff`, which lists the CLI-settable subset rather than the
    preset vocabulary -- it omits layer_height, so every SuperSlicer preset lost
    its layer height and every 0.2 nozzle failed to slice. An incomplete
    allow-list deletes silently; a deny-list of observed refusals cannot.
    """
    bad = reject or set()
    return collections.OrderedDict(
        (k, val) for k, val in kv.items()
        if not k.startswith(ANNOTATION_PREFIX) and k not in bad)


def chamber_block(target):
    """The chamber lines, in each slicer's own dialect, for the same behaviour.

    All three must do the same two things: BLOCK until the chamber reaches the
    material's minimum, then leave a non-blocking ceiling at its target. They
    cannot be told to in the same words.

      orca  emits M191 and M141 itself from activate_chamber_temp_control, so
            this block is a comment. See tools/start_gcode.py.
      ps    has chamber_minimal_temperature AND chamber_temperature natively.
      ss    has chamber_temperature but NO chamber_minimal_temperature -- the
            option does not exist, which is why ss-rejected-keys.json drops it.
            The minimum travels in filament_custom_variables instead, which is
            SuperSlicer's own mechanism for carrying a value into custom G-code.
            Seeded on every filament base, so the name can never be undefined.

    Until 2026-09-07 this was one line, `M141 S...`, and NO M191 on any slicer
    but Orca. A PrusaSlicer job therefore started heating immediately, never
    waited for the chamber and never got the bed boost -- observed on a live
    print, and the reason this function exists.
    """
    if target == "orca":
        return ";  chamber: Orca emits M191/M141 itself from its filament fields"
    if target == "ps":
        return "\n".join([
            ";  M191 blocks until the chamber reaches the material's minimum;",
            ";  M141 then leaves a non-blocking ceiling at its target.",
            "{if chamber_minimal_temperature[0] > 0}M191 S{chamber_minimal_temperature[0]}",
            "{endif}{if chamber_temperature[0] > 0}M141 S{chamber_temperature[0]}"
            "{else}M141 S32{endif}",
        ])
    return "\n".join([
        ";  SuperSlicer has no chamber_minimal_temperature. The minimum comes",
        ";  from filament_custom_variables, seeded on every filament base.",
        "{if chamber_temperature[0] > 0}M191 S{chamber_min}",
        "M141 S{chamber_temperature[0]}{else}M141 S32{endif}",
    ])


def inject_chamber(secs, target):
    n = 0
    body = chamber_block(target)
    for sec in secs:
        g = secs[sec].get("start_gcode")
        if not g or ";>>>CHAMBER" not in g:
            continue
        new = re.sub(r";>>>CHAMBER.*?;<<<CHAMBER",
                     lambda m: ";>>>CHAMBER emitted per slicer -- do not hand-edit\\n"
                               + body + "\\n;<<<CHAMBER", g)
        if new != g:
            secs[sec]["start_gcode"] = new
            n += 1
    return n


def strip_hidden(model, secs):
    """Drop tiers the model marks `hidden` from an OUTGOING artefact.

    The Stock tier is Sovol's published numbers, carried verbatim so the other
    tiers can be diffed against something real. docs/STATUS.md has said since it
    was written that it "builds but does not ship" -- and it shipped anyway, in
    all three bundles, as a base section with ZERO inheritors. Dead weight that
    redistributes a vendor's profile for no benefit to anyone who downloads it.

    Filtered on the way OUT, not removed from the master: the whole point of the
    tier is to be available locally for comparison.
    """
    hide = {"print:*SV Zero - %s*" % t
            for t, v in model["tiers"].items()
            if isinstance(v, dict) and v.get("hidden")}
    if not hide:
        return secs, 0
    kept = collections.OrderedDict(
        (k, v) for k, v in secs.items() if k not in hide)
    orphan = [k for k, v in kept.items()
              if ("print:%s" % (v.get("inherits") or "").strip()) in hide]
    if orphan:
        raise SystemExit("hidden tier %s still has inheritors: %s"
                         % (sorted(hide), orphan))
    return kept, len(secs) - len(kept)


def write_ini(path, secs, header, target=None, reject=None):
    """Write a bundle, stamped with the schema it targets.

    Real PrusaSlicer bundles carry `# generated by PrusaSlicer <ver>` on line 1.
    Ours declared nothing, so a future schema change — PrusaSlicer 3 is the
    obvious candidate — would surface as a puzzling import failure rather than a
    visible version mismatch.
    """
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("# %s\n" % header)
        if target:
            fh.write("# targets %s %s\n" % (target[0], target[1]))
        fh.write("# GENERATED by tools/generate.py — edit source/presets.json, not this file.\n\n")
        for name, kv in secs.items():
            fh.write("[%s]\n" % name)
            for k, v in ini_writable(kv, reject).items():
                fh.write("%s = %s\n" % (k, ini_value(v)))
            fh.write("\n")


def check_fragment_schema(doc):
    """Validate source/presets.json against PrusaSlicer 3.0's own schema.

    Not a resemblance to the new format -- a conformance check against the file
    Prusa ships in specs/presets/, vendored under source/ps3/ with its
    provenance. The schema is small enough to read directly (every object is
    `additionalProperties: false` over a fixed property list), so this needs no
    jsonschema dependency, and reading the shipped file rather than hardcoding
    the field names means a schema change shows up here rather than in an import
    failure months later.

    Structural only. The schema says nothing about which option names are valid
    inside `values` -- that is the key space, and it is checked elsewhere.
    """
    sch = json.load(open(os.path.join(ROOT, "source", "ps3", "preset-schema.json"),
                         encoding="utf-8"))
    defs = sch["$defs"]
    frag_props = set(defs["fragment"]["properties"])
    cond_props = frag_props | set(defs["conditional-fragment"]["properties"])
    top_props = cond_props | set(defs["top-fragment"]["properties"])
    kinds = set(defs["top-fragment"]["properties"]["kind"]["enum"])
    modes = set(defs["fragment"]["properties"]["match_mode"]["enum"])
    problems = []

    def node(n, path, props):
        for k in n:
            if k not in props:
                problems.append("%s: property %r not in the schema" % (path, k))
        if "match_mode" in n and n["match_mode"] not in modes:
            problems.append("%s: match_mode %r" % (path, n["match_mode"]))
        for k in ("inherits", "unconditional_inherits"):
            if k in n and not (isinstance(n[k], list)
                               and all(isinstance(x, str) for x in n[k])):
                problems.append("%s: %s must be a list of strings" % (path, k))
        for k in ("values", "features"):
            if k in n and not isinstance(n[k], dict):
                problems.append("%s: %s must be an object" % (path, k))
        for i, child in enumerate(n.get("variants", [])):
            node(child, "%s/%s" % (path, child.get("id", i)), cond_props)

    for frag in doc["fragments"]:
        if frag.get("kind") not in kinds:
            problems.append("%s: kind %r is not one of %s"
                            % (frag.get("id"), frag.get("kind"), sorted(kinds)))
        node(frag, frag.get("id", "?"), top_props)
    return problems


def nozzle_of(condition):
    """`tool.nozzle_diameter == 0.4` -> "0.4". The only condition dialect we author."""
    m = re.search(r"tool\.nozzle_diameter\s*==\s*([\d.]+)", condition or "")
    return m.group(1) if m else None


def sections_from_tree(doc, model):
    """Render source/presets.json into the flat {`kind:name` -> values} view.

    THIS IS THE ONE PLACE THE TREE BECOMES A SLICER'S IDEA OF A PRESET. Every
    emitter downstream still consumes the flat sections it always did, so the
    master moving out of SuperSlicer's .ini costs them nothing.

    Three things stop being stored and start being rendered:

      `inherits`            the node's position in the tree (or its explicit
                            `inherits`, where the tree branches on nozzle but
                            the values come from a tier base -- the same split
                            PrusaSlicer 3.0 makes between a *common* fragment
                            and the model file).
      `compatible_printers` the node's `condition`. One nozzle expression
                            renders as a 2.x printer-preset name here and as a
                            branch condition in 3.0.
      nozzle diameter       a `features` entry, because it is the free
                            parameter -- not a value that happens to differ.

    A node emits a section iff it has `values` or a `name`. Unnamed valueless
    nodes are pure structure: they carry a condition down to their children and
    nothing else.
    """
    printers = []
    for frag in doc["fragments"]:
        if frag["kind"] == "printer":
            printers += [v["name"] for v in frag.get("variants", []) if v.get("name")]
    out = collections.OrderedDict()

    def walk(node, kind, parent, condition):
        condition = node.get("condition", condition)
        name = node.get("name") or node.get("id")
        parent_for_children = parent
        if "values" in node or "name" in node:
            kv = collections.OrderedDict()
            inh = node.get("inherits") or ([parent] if parent else [])
            if inh:
                kv["inherits"] = inh[0]
            feats = node.get("features") or {}
            if kind == "printer" and "nozzle_diameter" in feats:
                kv["nozzle_diameter"] = feats["nozzle_diameter"]
                kv["printer_variant"] = feats["nozzle_diameter"]
            for k, v in node.get("values", {}).items():
                # Filaments declare compatibility with the whole printer set, so
                # it is derived from the printers rather than repeated per
                # material -- adding a nozzle used to mean editing 23 filaments.
                if kind == "filament" and k == "compatible_printers_condition":
                    kv["compatible_printers"] = ";".join(printers)
                kv[k] = v
            noz = nozzle_of(condition)
            if kind == "print" and noz and not name.startswith("*"):
                kv["compatible_printers"] = "SV Zero %sn" % noz
            out["%s:%s" % (kind, name)] = kv
            parent_for_children = name
        for child in node.get("variants", []):
            walk(child, kind, parent_for_children, condition)

    for frag in doc["fragments"]:
        walk(frag, frag["kind"], None, None)
    return out


def all_tiers(model):
    """Ramp rungs plus off-ramp variants — every tier that yields presets.

    `_order` is the RAMP and only the ramp: it is indexed and compared pairwise,
    so anything in it must be monotonic. A variant sits on a rung it names
    (`anchor_tier`) rather than between two of them, so it belongs to preset
    generation and to nothing that measures a ramp.
    """
    return list(model["tiers"]["_order"]) + list(model["tiers"].get("_variants", []))


def expected_presets(model):
    """The preset tree the model implies: (nozzle, tier, layer_height)."""
    out = []
    for noz in model["machine"]["nozzles"]:
        gaps = set(model["tier_gaps"].get(noz, []))
        for tier in all_tiers(model):
            if tier in gaps:
                continue
            lh = model["layer_heights"][noz].get(tier)
            if lh is None:
                continue
            out.append((noz, tier, lh))
    return out


def check(model, ss):
    """Does the master bundle match the model?"""
    problems = []
    want = {(n, t) for n, t, _ in expected_presets(model)}
    have = {}
    for name in ss:
        m = re.match(r"print:SV Zero (\d\.\d)n - ([\d.]+)mm \((\w+)\)", name)
        if m:
            have[(m.group(1), m.group(3))] = (float(m.group(2)), name)

    for key in sorted(want - set(have)):
        problems.append("missing preset: %s nozzle, %s tier" % key)
    for key in sorted(set(have) - want):
        problems.append("unexpected preset: %s nozzle, %s tier" % key)
    for (noz, tier), (lh, name) in sorted(have.items()):
        if (noz, tier) not in want:
            continue
        wlh = model["layer_heights"][noz][tier]
        if abs(lh - wlh) > 1e-9:
            problems.append("%s: layer height %s, model says %s" % (name, lh, wlh))

    problems.extend(check_layer_limits(model, ss))

    for tier in all_tiers(model):
        if not isinstance(model["tiers"].get(tier), dict):
            continue
        if "sqv" not in model["tiers"][tier]:
            problems.append("tier %s has no sqv" % tier)

    # Tiers are an ordered ramp: Fine slowest, Draft fastest. Any speed
    # that goes backwards along that order is a defect, and it is invisible in
    # the INI because the tiers express speed inconsistently -- some absolute,
    # some as a percentage of a per-tier `default_speed`. Resolve, then compare.
    ramp = collections.defaultdict(list)
    for tier in model["tiers"]["_order"]:
        if model["tiers"].get(tier, {}).get("exempt_from_ramp"):
            continue
        sec = "print:*SV Zero - %s*" % tier
        if sec not in ss:
            continue
        flat, cur, seen = {}, sec, set()
        while cur and cur in ss and cur not in seen:
            seen.add(cur)
            for k, v in ss[cur].items():
                flat.setdefault(k, v)
            par = ss[cur].get("inherits", "").strip()
            cur = "print:%s" % par if par else None
        r = resolve_speeds(flat)
        for k in ("perimeter_speed", "external_perimeter_speed", "infill_speed"):
            try:
                ramp[k].append((tier, float(r[k])))
            except (KeyError, TypeError, ValueError):
                pass
    for k, seq in sorted(ramp.items()):
        for (t1, v1), (t2, v2) in zip(seq, seq[1:]):
            if v2 < v1:
                problems.append("%s goes backwards: %s=%g then %s=%g "
                                "(tiers must ramp Fine->Draft)" % (k, t1, v1, t2, v2))
    lim = model.get("machine", {}).get("limits", {})
    cap = lim.get("max_accel")
    if cap:
        for name, kv in ss.items():
            if not name.startswith("print:"):
                continue
            for k, v in kv.items():
                if not k.endswith("_acceleration"):
                    continue
                try:
                    val = float(str(v).strip().rstrip("%"))
                except ValueError:
                    continue
                if not str(v).strip().endswith("%") and val > cap:
                    problems.append("%s: %s = %g exceeds machine max_accel %g"
                                    % (name, k, val, cap))
    return problems


def emit_stock_tier(model, ss, keymap, anchors):
    """Build *SV Zero - Stock* verbatim from Sovol's published values.

    Deliberately NOT reconciled with the proportional model. Stock is the thing
    the other tiers are measured against, so making it consistent would destroy
    the only property that matters about it. Expect it to violate the ramp:
    Sovol asks outer 350 / inner 400 / infill 500 with no coherent ratio.

    Sourced from Sovol's actual shipped Orca profile
    (source/sovol-stock-process.json), not from the forum comparison table. The
    table is a derived artefact and is incomplete — it omits sparse_infill_speed
    entirely, so a Stock tier built from it silently lacked the 500 mm/s infill
    that is arguably stock's most characteristic number. The anchors remain the
    reference for the Speed tier and for diffing.

    Orca names go through the keymap.
    """
    fwd = keymap["mapped"]
    sec = collections.OrderedDict()
    sec["inherits"] = "*SV Zero General*"
    skipped = []
    src = json.load(open(os.path.join(ROOT, "source", "sovol-stock-process.json"),
                         encoding="utf-8"))
    for orca_key in sorted(src):
        val = src[orca_key]
        if isinstance(val, list):
            val = val[0] if len(val) == 1 else None
        if val is None or val == "" or orca_key.startswith(("compatible", "from", "type",
                                                            "name", "inherits", "instantiation")):
            continue
        target = fwd.get(orca_key)
        if not target:
            skipped.append(orca_key)
            continue
        # Enum vocabularies differ between slicers even where the key name is
        # shared. PrusaSlicer has no `auto_brim`; left alone it warns on every
        # launch and silently substitutes.
        vm = keymap.get("value_map", {}).get(orca_key, {})
        sec[target] = vm.get(str(val), val)
    # The machine ceiling is stated once in *General*; a reference tier must not
    # quietly redefine it.
    sec.pop("max_volumetric_speed", None)
    # Insert alongside the other tier bases, not appended after the filaments —
    # section order does not change behaviour but a bundle nobody can read is a
    # bundle nobody will maintain.
    # Drop any previous Stock section FIRST. Without this the rebuild loop below
    # re-inserts the stale one when it reaches it, silently overwriting the fresh
    # section and making the generator non-idempotent — it produced correct output
    # on a clean bundle and stale output on every re-run.
    ss.pop("print:*SV Zero - Stock*", None)
    rebuilt, placed = collections.OrderedDict(), False
    for name, kv in ss.items():
        if not placed and name.startswith("print:SV Zero "):
            rebuilt["print:*SV Zero - Stock*"] = sec
            placed = True
        rebuilt[name] = kv
    if not placed:
        rebuilt["print:*SV Zero - Stock*"] = sec
    ss.clear()
    ss.update(rebuilt)
    return len(sec) - 1, skipped


def apply_speed_model(model, ss):
    """Write the proportional speed model into the master bundle.

    Collapses every tier speed onto one number per tier. Uniform ratios live in
    *SV Zero General* so they cannot diverge between tiers; each tier base
    carries only `default_speed`. Before this, three tiers agreed to within
    rounding while the fastest rung expressed everything as percentages of a default_speed
    that sat *below* Normal's — which is how infill ended up running backwards
    along the ramp.
    """
    sm = model.get("speed_model")
    if not sm:
        return 0
    changed = 0
    gen = ss.get("print:*SV Zero General*")
    if gen is not None:
        # Machine capability, stated once. PrusaSlicer/SuperSlicer take the LOWER
        # of this and filament_max_volumetric_speed, so machine and material stay
        # separable and no tier needs hand-capping.
        mv = str(model["machine"].get("max_volumetric_speed", 0))
        if gen.get("max_volumetric_speed") != mv:
            gen["max_volumetric_speed"] = mv; changed += 1
        for k, v in list(sm["ratios_of_base"].items()) + list(sm["ratios_of_other"].items()):
            if gen.get(k) != v:
                gen[k] = v; changed += 1
    for tier, base in sm["base_speed"].items():
        sec = ss.get("print:*SV Zero - %s*" % tier)
        if sec is None:
            continue
        if sec.get("default_speed") != str(base):
            sec["default_speed"] = str(base); changed += 1
        # Per-tier speed overrides are what allowed the tiers to drift; the
        # ratios in *General* are now the only source.
        for k in list(sm["ratios_of_base"]) + list(sm["ratios_of_other"]):
            if k in sec:
                del sec[k]; changed += 1
    return changed


def emit_physical_printers(model, ss):
    """PrusaSlicer [physical_printer:] sections, so upload needs no hand-typed IP.

    Format taken from a real PrusaSlicer export (Tom's Print Garden's bundle)
    rather than guessed. PrusaSlicer has a native `moonraker` host_type; Orca
    does not and uses `octoprint` against the same endpoint — see hosts in the
    model.
    """
    hosts = model.get("hosts")
    if not hosts or not PERSONAL:
        # A [physical_printer:] section exists ONLY to carry an address, so
        # there is nothing to emit for a published bundle -- not a blank one,
        # which would just be a broken entry in the user's printer list.
        # Somebody installing the pack adds their own printer once, in the
        # dialog built for it.
        return 0
    printers = [n for n in ss if n.startswith("printer:") and not n.startswith("printer:*")]
    names = [p.split(":", 1)[1] for p in printers]
    # Default to the 0.4 nozzle, not whichever sorts first — 0.4 is what ships on
    # the machine and the only nozzle Sovol supplies a stock process for.
    default_preset = next((n for n in names if "0.4n" in n), names[0] if names else "SV Zero 0.4n")
    n = 0
    for pr in hosts["printers"]:
        sec = collections.OrderedDict()
        sec["host_type"] = hosts["prusaslicer_host_type"]
        sec["preset_name"] = default_preset
        sec["preset_names"] = ";".join(names)
        sec["print_host"] = pr["host"]
        sec["printer_technology"] = "FFF"
        sec["printhost_apikey"] = ""
        sec["printhost_authorization_type"] = "key"
        sec["printhost_port"] = ""
        ss["physical_printer:%s" % pr["name"]] = sec
        n += 1
    return n


def apply_flow_model(model, ss):
    """Derive each concrete preset's default_speed from its geometry and flow.

    A tier cannot be one set of speeds: volumetric demand is speed x width x
    layer, and both width and layer scale with nozzle, so the same infill speed
    demands 7.9 mm3/s on a 0.2 nozzle and 157 on a 1.0. The tier holds ratios;
    the preset holds the one number that anchors them.

    Not left to the slicer's clamp, because clamping inverts feature ordering
    rather than preserving it — see flow_model._why_not_clamping in the model.
    """
    fm = model.get("flow_model")
    if not fm:
        return 0, []
    wr, cap = fm["infill_width_ratio"], float(fm["speed_cap"])
    changed, report = 0, []
    for name, kv in ss.items():
        m = re.match(r"print:SV Zero (\d\.\d)n - ([\d.]+)mm \((\w+)\)", name)
        if not m:
            continue
        noz, tier = float(m.group(1)), m.group(3)
        lh = float(kv.get("layer_height", m.group(2)))
        flow = fm["reference_flow"].get(tier)
        if not isinstance(flow, (int, float)):
            continue
        util = fm.get("flow_utilisation", {}).get(tier, 1.0)
        want = (flow * util) / (wr * noz * lh)
        limited = "flow"
        if want > cap:
            want, limited = cap, "motion"
        val = computed_motion(want)
        if kv.get("default_speed") != val:
            kv["default_speed"] = val
            changed += 1
        report.append((name, tier, noz, lh, float(val), flow * util,
                       float(val) * wr * noz * lh, limited))
    # The meaningful invariant is no longer raw speed — under the flow model a
    # thinner layer legitimately runs faster for the same flow. What must ramp is
    # volumetric utilisation.
    # Ramp rungs only. A variant shares its anchor's rung, so it has no position
    # in this sequence and comparing it against one is meaningless -- and
    # `.index()` on a tier that is not in `_order` is an outright crash.
    order = model["tiers"]["_order"]
    by_noz = collections.defaultdict(list)
    for name, tier, noz, lh, spd, target, actual, limited in report:
        if tier not in order:
            continue
        by_noz[noz].append((order.index(tier), tier, actual, limited))
    for noz, seq in sorted(by_noz.items()):
        seq.sort()
        for (_, t1, v1, l1), (_, t2, v2, l2) in zip(seq, seq[1:]):
            if v2 < v1 - 1e-6 and "motion" not in (l1, l2):
                report.append(("RAMP", "%s nozzle: %s uses %.1f mm3/s then %s uses %.1f"
                               % (noz, t1, v1, t2, v2), 0, 0, 0, 0, 0, "error"))
    return changed, report


PS_TARGET = "2.9.6"          # overwritten from model.json in main()
PS_CONFIG_VERSION = "2.0.3"  # vendor bundle version; must match the .idx entry
PS_MIN_VERSION = "2.6.0"     # .idx gate -- above the installed PS, the vendor is ignored


# Keys SuperSlicer has and PrusaSlicer rejects. PrusaSlicer does not merely warn:
# it strips the key and marks the whole Vendor Config Bundle as errored.
#
#   Error in a Vendor Config Bundle ".../SVZero.ini": The printer preset
#   "printer:SV Zero 0.2n" contains the following incorrect keys: arc_fitting,
#   which were removed
#
# Only visible with --loglevel 4; the GUI shows nothing. Add keys here as they
# appear, and check the printer presets as well as the print ones -- the message
# names every concrete preset that inherits the base carrying the key, not the
# base itself.
SS_ONLY = {"arc_fitting", "arc_fitting_resolution", "arc_fitting_tolerance",
           # SuperSlicer filament keys PrusaSlicer does not define. Checked
           # against the 2.9.6 binary's own symbol table, not assumed:
           #   filament_pressure_advance  absent (PS has no PA field at all)
           # PA is recovered below as start_filament_gcode. filament_shrink is
           # translated rather than dropped -- see apply_shrinkage: the two
           # spellings differ, and the direction is now settled from
           # PrusaSlicer's own tooltip rather than assumed.
           "filament_pressure_advance", "filament_shrink"}

PS_VENDOR_ID = "SVZero"          # the .ini/.idx basename, and the PrusaSlicer.ini key
PS_PRINTER_MODEL = "SVZERO"      # printer_model id; uppercase by convention


def apply_shrinkage(ss, data):
    """Give every filament a real shrinkage figure instead of 100%.

    Every vendor in the OrcaSlicer profile tree ships filament_shrink at 100% --
    64 files, not one populated. That is an unfilled default, not a claim that
    plastics do not shrink, and it means every part comes out systematically
    undersized by the material's shrinkage.

    The values are nominal polymer-class typicals, not measurements; see
    source/filament-shrink.json and the runbook it points at.
    """
    mats = data["materials"]
    n = 0
    for name in ss:
        if not name.startswith("filament:"):
            continue
        kv = ss[name]
        ftype = flatten(ss, name).get("filament_type")
        # "PLA Silk" is carried in the preset name, not filament_type, which
        # says PLA for both.
        key = "PLA Silk" if "Silk" in name else ftype
        m = mats.get(key) or mats.get(ftype)
        if not m:
            continue
        kv["filament_shrink"] = m["shrink"]
        n += 1
    return n


def shrink_to_ps(value):
    """Orca/SuperSlicer scale percentage -> PrusaSlicer shrinkage percentage.

    They express the same physical quantity two different ways, and getting the
    direction wrong silently distorts every dimension in the opposite sense.

      SuperSlicer / Orca  filament_shrink                  = SCALE factor.
                          100% is no compensation, 100.3% scales up 0.3%.
      PrusaSlicer         filament_shrinkage_compensation_xy = the SHRINKAGE.
                          Its own tooltip: "if you measured 99mm instead of
                          100mm, enter 1%". So 0% is no compensation.

    Hence PS = ours - 100. Read from the 2.9.6 binary's own strings, not guessed.
    """
    try:
        return "%g%%" % (float(str(value).strip().rstrip("%")) - 100.0)
    except ValueError:
        return None


def apply_8m_overrides(ss, data):
    """Bring the (8M) presets up to the SV08 MAX's toolhead-scope values.

    (8M) began as a curation of the Max's profiles into the Zero's, but only two
    settings ever came across -- filament_max_volumetric_speed and the minimum
    layer time. Assessed in notes/kb/filament-profiles.md against the toolhead
    lineage, the group that *should* transfer is the one that did not: the Zero
    and the Max share a toolhead outright, so temperature, pressure advance, flow
    ratio and retraction are properties of the same hardware. Cooling, bed and
    exhaust values stay excluded -- a 152 mm enclosure that reaches 43 C is not a
    500 mm machine.

    Values come from source/filament-8m.json, extracted mechanically from Sovol's
    own Max profiles. They are Sovol's numbers, not measurements of ours.
    """
    n = 0
    for preset, kv in data["overrides"].items():
        sec = "filament:%s" % preset
        if sec not in ss:
            continue
        for k, v in kv.items():
            ss[sec][k] = v
        note = ss[sec].get("filament_notes", "")
        tag = "[8M]: toolhead-scope values from SV08 MAX (shared toolhead)"
        if "toolhead-scope" not in note:
            base = note.split(" | [8M]")[0]
            ss[sec]["filament_notes"] = "%s | %s" % (base, tag)
        n += 1
    return n


def flatten(ss, name):
    """Resolve a preset's whole `inherits` chain into one flat dict."""
    flat, cur, seen = collections.OrderedDict(), name, set()
    kind = name.split(":", 1)[0]
    while cur and cur in ss and cur not in seen:
        seen.add(cur)
        for k, v in ss[cur].items():
            if k != "inherits":
                flat.setdefault(k, v)
        par = ss[cur].get("inherits", "").strip()
        cur = "%s:%s" % (kind, par) if par else None
    return flat


def layer_mm(value, nozzle):
    """Resolve authored millimetres or percent-of-nozzle without rounding."""
    value = str(value).strip()
    result = (float(value[:-1]) * float(nozzle) / 100
              if value.endswith("%") else float(value))
    if not math.isfinite(result) or result <= 0:
        raise ValueError("layer height must be finite and positive")
    return result


def printer_layer_limits(ss, nozzle):
    """One source for both Orca machine emitters, including inherited values."""
    printer = flatten(ss, "printer:SV Zero %sn" % nozzle)
    return {key: "%g" % layer_mm(printer[key], nozzle)
            for key in ("min_layer_height", "max_layer_height")}


def check_layer_limits(model, ss):
    """Reject incompatible authored layers before overwriting any bundles."""
    problems = []
    limits = {}
    for nozzle in model["machine"]["nozzles"]:
        try:
            bounds = printer_layer_limits(ss, nozzle)
            low, high = (float(bounds[k]) for k in ("min_layer_height", "max_layer_height"))
            if not low <= high <= float(nozzle):
                raise ValueError("require min <= max <= nozzle diameter")
            limits[nozzle] = (low, high)
        except (KeyError, TypeError, ValueError) as error:
            problems.append("%s nozzle: invalid layer limits (%s)" % (nozzle, error))
    for name in ss:
        match = re.match(r"print:SV Zero ([\d.]+)n - ", name)
        if not match or match.group(1) not in limits:
            continue
        nozzle = match.group(1)
        low, high = limits[nozzle]
        flat = flatten(ss, name)
        for key in ("layer_height", "first_layer_height"):
            try:
                height = layer_mm(flat[key], nozzle)
                if not low - 1e-9 <= height <= high + 1e-9:
                    problems.append("%s: %s=%g outside layer limits %g..%g"
                                    % (name, key, height, low, high))
            except (KeyError, TypeError, ValueError) as error:
                problems.append("%s: invalid %s (%s)" % (name, key, error))
    return problems


# PrusaSlicer takes these three in MILLIMETRES. SuperSlicer accepts a percentage
# of nozzle diameter and the master is written that way, so a straight copy hands
# PrusaSlicer "80%" for a millimetre field. The plater then refuses to slice with
# a bare "Invalid data" and nothing in the log, which is why this took a GUI
# report to find.
#
# Checked against PrusaSlicer's own vendor bundle: every one of the 100+
# min_layer_height / max_layer_height / first_layer_height values it ships is an
# absolute number, never a percentage.
#
# The basis is the nozzle diameter, confirmed rather than assumed: 62.5% of the
# 0.4 nozzle is 0.25, which is exactly Sovol's own initial_layer_print_height for
# the 0.20 mm profile.
PS_MM_FROM_NOZZLE_PCT = ("first_layer_height", "min_layer_height", "max_layer_height")

# PrusaSlicer resolves an extrusion-width percentage against the LAYER HEIGHT,
# not the nozzle. SuperSlicer's master means percent-of-nozzle, so 100% at a
# 0.2 mm layer arrived as 0.2 mm and the plater refused outright:
#
#   perimeter_extrusion_width=0.2 mm is too low to be printable at a
#   layer height 0.2 mm
#
# PrusaSlicer's own bundle never uses a percentage for any of these -- 82, 85, 73
# and 54 absolute values against zero percentages -- so they are converted
# against the nozzle, which is the basis the master intends.
PS_MM_WIDTH_SUFFIX = "extrusion_width"
PS_RATIO_FROM_PCT = ("bridge_flow_ratio",)

# WAS first_layer_speed, and is now empty. Kept as the mechanism, because the
# reason it existed can come back: custom G-code cannot reference a config value
# that is still a percentage -- PrusaSlicer has no base to resolve it against
# inside a macro and aborts the export with
#
#   Parsing error at line 35: FloatOrPercent variable failed to resolve the
#   "ratio_over" ... local svz_prime_f = 60 * first_layer_speed;
#
# start_gcode no longer references first_layer_speed (the prime moved into
# PURGE_LINE), so the constraint is gone -- and absolutising it was never free.
# Measured 2026-09-01: `first_layer_speed = 10.5%` makes BOTH slicers scale each
# feature's own speed (external perimeter 13.8, perimeter 16.7 mm/s), while the
# absolute 24.4965 flattens the whole first layer to one number and makes the
# external perimeter the FASTEST thing on it. Percentage preserved; the two
# slicers now emit identical first layers.
PS_SPEED_FROM_DEFAULT_PCT = ()

# first_layer_speed is a MULTIPLIER, not a reference to a base: both slicers
# document the percentage as scaling "the current speed" / "the default speeds"
# per feature, so 10.5% means every first-layer move runs at a tenth of its own
# speed. The catch-all in ps_absolutise_speeds would resolve it against
# default_speed and flatten the whole first layer to one number -- which made
# the external perimeter the FASTEST thing on the layer instead of the slowest.
# Measured both ways on 2026-09-01; as a percentage PrusaSlicer and SuperSlicer
# emit an identical first layer.
# first_layer_speed stays a percentage, deliberately.
#
# PrusaSlicer and SuperSlicer both read it as a per-feature MULTIPLIER, which
# gives a GRADED first layer: every feature keeps its ratio to the others, so
# the external perimeter stays slower than the internal one exactly as it does
# on every other layer. Orca has one absolute field and cannot express that.
#
# Absolutising it to a flat 24.5 mm/s was tried on 2026-09-07 to make all three
# identical, and reverted the same day: matching the weakest dialect throws away
# a control the other two have, which is lowest-common-denominator rather than
# consistency. Orca instead approximates the curve with the two fields it does
# have -- see ORCA_FIRST_LAYER.
#
# The 1.575 mm/s that started all this was never this key's fault. It was an
# unpinned small_perimeter_speed handing the multiplier a 15 mm/s base.
PS_KEEP_PERCENT = ("first_layer_speed",)

# The baseline fan speed has three names and every one of the three slicers uses
# a different one. Verified against the installed binaries on 2026-09-01, not
# assumed:
#
#   SuperSlicer 2.7.62   default_fan_speed   "speed for features where there is
#                                             no fan control"
#   PrusaSlicer 2.9.6    min_fan_speed
#   OrcaSlicer 2.4.2     fan_min_speed       (source/keymap.json)
#
# The master carries SuperSlicer's spelling. The filament presets were imported
# from Orca JSON and kept ORCA's, which meant every slicer silently ignored the
# fan floor: PrusaSlicer strips an unknown key, and SuperSlicer drops
# min_fan_speed with NO warning at all -- it is simply absent from the config
# echoed into the G-code footer, which is how tools/slice-harness.py caught it.
PS_RENAME = {"default_fan_speed": "min_fan_speed"}


def ps_percent_to_mm(kv, preset):
    m = re.search(r"SV Zero (\d+\.\d+)n", preset)
    if not m:
        return 0
    noz, n = float(m.group(1)), 0
    for k, v in list(kv.items()):
        if not isinstance(v, str) or not v.strip().endswith("%"):
            continue
        try:
            pct = float(v.strip()[:-1])
        except ValueError:
            continue
        if k in PS_MM_FROM_NOZZLE_PCT or k.endswith(PS_MM_WIDTH_SUFFIX):
            kv[k] = "%g" % (pct / 100.0 * noz); n += 1
        elif k in PS_RATIO_FROM_PCT:
            kv[k] = "%g" % (pct / 100.0); n += 1
        elif k in PS_SPEED_FROM_DEFAULT_PCT:
            try:
                base = float(str(kv.get("default_speed", "")).rstrip("%"))
            except ValueError:
                continue
            if base:
                kv[k] = computed_motion(pct / 100.0 * base); n += 1
    return n


def ps_absolutise_speeds(kv):
    """Resolve every speed and acceleration percentage for PrusaSlicer.

    THIS is why a Benchy sliced at 2h27 in PrusaSlicer against 33 minutes in
    Orca. SuperSlicer expresses most speeds as a percentage of `default_speed`,
    its own invention -- and PrusaSlicer has no such option, confirmed from
    `prusa-slicer --help-fff`. So "perimeter_speed = 68%" resolved against
    nothing and PrusaSlicer read roughly 68 mm/s where the master meant 68% of
    233.3, i.e. 158.6.

    The percentages that survive are worse than the ones that fail outright,
    because PrusaSlicer DOES accept a percentage on external_perimeter_speed,
    solid_infill_speed and top_solid_infill_speed -- over a different base than
    SuperSlicer means. Name-correct, unit-wrong, and it slices happily.

    Accelerations get the same treatment: PrusaSlicer's own bundle uses 201
    absolute values for perimeter_acceleration and no percentages.
    """
    flat = resolve_speeds(kv)          # walks the PCT_REFERENCE chain
    kv.update({k: v for k, v in flat.items() if k in kv})

    def base(key):
        try:
            return float(str(kv.get(key, "")).strip().rstrip("%"))
        except ValueError:
            return 0.0
    speed, accel = base("default_speed"), base("default_acceleration")
    n = 0
    for k, v in list(kv.items()):
        if not isinstance(v, str) or not v.strip().endswith("%"):
            continue
        if k in PS_KEEP_PERCENT:
            continue
        try:
            pct = float(v.strip()[:-1])
        except ValueError:
            continue
        if k.endswith("_speed") and speed:
            kv[k] = computed_motion(pct / 100.0 * speed); n += 1
        elif k.endswith("_acceleration") and accel:
            kv[k] = computed_motion(pct / 100.0 * accel); n += 1
    return n


PS_KNOWN = set()          # filled in main() from source/ps-known-options.json
SS_REJECT = set()         # filled in main() from source/ss-rejected-keys.json
# Set by main() from --personal. False is the publishable build, and it is the
# default precisely because the failure mode of the other default is shipping a
# stranger a profile pointing at <printer-address> on their own network.
PERSONAL = False


def emit_ps_presets(ss, out_dir):
    """Split the bundle into per-preset .ini files, PrusaSlicer's on-disk form.

    A config *bundle* only imports through the GUI. PrusaSlicer's actual storage
    is one flat .ini per preset under <datadir>/{print,filament,printer,
    physical_printer}/, read at startup. Writing those directly removes the
    import dialog, which is the difference between a pipeline and a chore.

    THESE ARE FLATTENED, AND THE `*Base*` PRESETS ARE NOT WRITTEN AT ALL.

    User presets cannot express our preset tree. `*Asterisk*` is not a naming
    convention the UI hides everywhere -- it marks a non-instantiable *system*
    preset -- and a user preset may only inherit from a *system* preset. So as
    loose files the six bases showed up in the dropdown beside the 19 real
    presets, and the 19 were five-line stubs whose parents did not resolve.

    The obvious fix is a vendor bundle, and `--emit ps-vendor` writes a correct
    one. It is not usable here: PrusaSlicer 2.9.6 discards any `[vendor:...]`
    section written into PrusaSlicer.ini by hand, so enabling a vendor means
    clicking through the ConfigWizard on every fresh sandbox. Verified rather
    than assumed -- a hand-written `[vendor:PrusaResearch]` for a bundle
    PrusaSlicer ships itself was discarded exactly the same way, so this is
    PrusaSlicer's behaviour and not a defect in our bundle.

    Flattening sidesteps both problems: no bases to hide, nothing to inherit,
    no wizard. The tree still exists where it is worth having -- in
    source/model.json and the SuperSlicer master -- and these files are
    generated output, so losing the inheritance on disk costs nothing.
    """
    kinds = ("print", "filament", "printer", "physical_printer")
    for d in kinds:
        sub = os.path.join(out_dir, d)
        os.makedirs(sub, exist_ok=True)
        # Clear first. These are whole-directory output, and a preset that is
        # renamed rather than changed leaves its old file behind -- which
        # installs as a second, stale preset next to the real one. The Orca
        # emitter has always done this; this one had not, and the 2026-09-02
        # tier rename produced 19 orphans before it was noticed.
        for old in os.listdir(sub):
            if old.endswith(".ini"):
                os.remove(os.path.join(sub, old))
    written = 0
    for name in ss:
        if ":" not in name:
            continue
        kind, preset = name.split(":", 1)
        if kind not in kinds or preset.startswith("*"):
            continue
        kv = flatten(ss, name) if kind != "physical_printer" else ss[name]
        # PrusaSlicer has no pressure-advance field, so the value would simply be
        # lost. On a Klipper machine the right channel is the filament start
        # G-code, which is exactly what SET_PRESSURE_ADVANCE is for.
        if kind == "print":
            ps_absolutise_speeds(kv)
        for src, dst in PS_RENAME.items():
            if src in kv:
                kv.setdefault(dst, kv.pop(src))
        ps_percent_to_mm(kv, preset)
        sh = kv.get("filament_shrink")
        if kind == "filament" and sh:
            ps = shrink_to_ps(sh)
            if ps is not None:
                kv["filament_shrinkage_compensation_xy"] = ps
                kv["filament_shrinkage_compensation_z"] = "0%"
        pa = kv.get("filament_pressure_advance")
        if kind == "filament" and pa:
            g = kv.get("start_filament_gcode", "").strip()
            cmd = "SET_PRESSURE_ADVANCE ADVANCE=%s" % pa
            if "SET_PRESSURE_ADVANCE" not in g:
                kv["start_filament_gcode"] = (g + "\n" + cmd).strip() if g else cmd
        # Filter LAST. The translations above consume SuperSlicer-only source
        # keys -- filament_pressure_advance becomes start_filament_gcode,
        # filament_shrink becomes filament_shrinkage_compensation_xy -- so
        # filtering first silently deletes the input and the output is never
        # produced. That is exactly what happened: one commit shipped presets
        # with neither pressure advance nor shrinkage.
        kv = collections.OrderedDict(
            (k, v) for k, v in kv.items() if k in PS_KNOWN)
        path = os.path.join(out_dir, kind, "%s.ini" % preset)
        with open(path, "w", encoding="utf-8") as fh:
            for k, v in ini_writable(kv).items():
                if k not in SS_ONLY:
                    fh.write("%s = %s\n" % (k, ini_value(v)))
        written += 1
    return written


def emit_ss_presets(model, ss, out_dir):
    """Split the bundle into per-preset .ini files in SuperSlicer's OWN dialect.

    Same problem emit_ps_presets solves and the same shape of answer, with one
    difference that is the whole reason this is a separate function: **none of
    the PrusaSlicer translations run.**

    A config bundle imports only through the GUI, and the sandbox's job is to
    launch into a working printer rather than into a wizard. But the flattened
    PrusaSlicer presets are exactly what SuperSlicer must not be handed --
    `ps_percent_to_mm` resolves widths against the nozzle, `ps_absolutise_speeds`
    resolves speeds against `default_speed`, and the master means those
    percentages literally. Feeding SuperSlicer its own numbers already resolved
    would silently change every preset.

    So: flatten (user presets cannot inherit from other user presets, which is
    the one constraint both slicers share), drop the `*Base*` sections, and
    write the values through unchanged. No key filter either -- SuperSlicer's
    option set is the superset the master was authored in.
    """
    kinds = ("print", "filament", "printer", "physical_printer")
    pub, _ = strip_hidden(model, ss)
    pub = collections.OrderedDict((k, collections.OrderedDict(v)) for k, v in pub.items())
    inject_chamber(pub, "ss")
    for d in kinds:
        sub = os.path.join(out_dir, d)
        os.makedirs(sub, exist_ok=True)
        for old in os.listdir(sub):
            if old.endswith(".ini"):
                os.remove(os.path.join(sub, old))
    written = 0
    for name in pub:
        if ":" not in name:
            continue
        kind, preset = name.split(":", 1)
        if kind not in kinds or preset.startswith("*"):
            continue
        kv = flatten(pub, name) if kind != "physical_printer" else pub[name]
        # `inherits` is meaningless once flattened, and a user preset naming a
        # parent that is not a SYSTEM preset fails to resolve rather than being
        # ignored -- which is the bug that made these stubs in the first place.
        kv = collections.OrderedDict(
            (k, v) for k, v in kv.items() if k != "inherits")
        # SuperSlicer's own option set, from its own binary. Without it the
        # Orca-authored chamber keys reach every filament preset and SuperSlicer
        # reports them as unknown settings on load.
        kv = ini_writable(kv, SS_REJECT)
        path = os.path.join(out_dir, kind, "%s.ini" % preset)
        with open(path, "w", encoding="utf-8") as fh:
            for k, v in kv.items():
                fh.write("%s = %s\n" % (k, ini_value(v)))
        written += 1
    return written


def ps_bundle_resolve(ss):
    """Give the bundles the same three conversions the flattened presets get.

    Until 2026-09-02 they got none of them. `emit_ps_presets` ran
    `ps_absolutise_speeds`, `ps_percent_to_mm` and the `PS_KNOWN` filter; the two
    bundle emitters stripped `SS_ONLY` and nothing else. So the artefact people
    actually import -- the config bundle, and the vendor bundle behind it --
    shipped percent-of-nozzle widths into a field PrusaSlicer resolves against
    LAYER HEIGHT, percent-of-`default_speed` speeds against a `default_speed`
    PrusaSlicer does not have, and 89 keys it does not define. The loose presets
    were right and the published form was wrong, from the same master.

    The bundle cannot simply be flattened -- inheritance and hidden `*Base*`
    presets are the whole reason the vendor form exists. So the resolution is
    pushed DOWN: each concrete preset is flattened, converted, and any key the
    conversion changed is written onto that preset, where it overrides the base.
    The percentages left behind in the bases are then removed, because every
    concrete preset now carries an absolute value and a stale percentage in a
    base is a trap for the next preset added.

    Acceptance test, run in the harness: flattening this bundle must reproduce
    `bundles/ps-presets/` exactly.
    """
    out = collections.OrderedDict((n, collections.OrderedDict(kv))
                                  for n, kv in ss.items())

    # ---- print and printer: resolve per concrete preset, against its own
    # nozzle. Printers matter too -- min/max_layer_height are percent-of-nozzle
    # in the master and PrusaSlicer takes them in millimetres, and they live on
    # the nozzle-independent printer base where nothing can resolve them.
    resolved = set()
    for kind in ("print", "printer"):
        for name in list(out):
            if not name.startswith(kind + ":") or name.startswith(kind + ":*"):
                continue
            preset = name.split(":", 1)[1]
            flat = flatten(ss, name)
            kv = collections.OrderedDict(flat)
            if kind == "print":
                ps_absolutise_speeds(kv)
            ps_percent_to_mm(kv, preset)
            for k, v in kv.items():
                if str(flat.get(k)) != str(v):
                    out[name][k] = v
                    resolved.add(k)
        for name in out:
            if not name.startswith(kind + ":*"):
                continue
            for k in [k for k in out[name]
                      if k in resolved and str(out[name][k]).strip().endswith("%")]:
                del out[name][k]

    # ---- filament: the same translations, written where the key is declared.
    # Read through the chain, write to the section that owns it, so a value
    # inherited from a base is not silently re-declared on the child.
    for name in list(out):
        if not name.startswith("filament:"):
            continue
        own, flat = out[name], flatten(ss, name)
        sh = own.get("filament_shrink")
        if sh:
            ps = shrink_to_ps(sh)
            if ps is not None:
                own["filament_shrinkage_compensation_xy"] = ps
                own["filament_shrinkage_compensation_z"] = "0%"
        pa = own.get("filament_pressure_advance")
        if pa:
            g = flat.get("start_filament_gcode", "").strip()
            cmd = "SET_PRESSURE_ADVANCE ADVANCE=%s" % pa
            if "SET_PRESSURE_ADVANCE" not in g:
                own["start_filament_gcode"] = (g + "\n" + cmd).strip() if g else cmd

    # ---- renames, then the key filter. FILTER LAST -- the translations above
    # consume SuperSlicer-only source keys (filament_shrink becomes
    # filament_shrinkage_compensation_xy, filament_pressure_advance becomes
    # start_filament_gcode), so dropping SS_ONLY first deletes the input and the
    # output is never produced. The first version of this function did exactly
    # that and lost shrinkage and pressure advance from all 23 filaments.
    for name, kv in out.items():
        for src, dst in PS_RENAME.items():
            if src in kv:
                kv.setdefault(dst, kv.pop(src))
    dropped = 0
    for name in list(out):
        if name == "vendor" or name.startswith("printer_model:"):
            continue          # bundle structure, not slicing options
        before = len(out[name])
        out[name] = collections.OrderedDict(
            (k, v) for k, v in out[name].items()
            if k in PS_KNOWN and k not in SS_ONLY)
        dropped += before - len(out[name])
    return out, dropped


def emit_ps_vendor(model, ss, out_dir):
    """PrusaSlicer as a vendor bundle rather than a pile of user presets.

    Two things forced this, and they are the same thing seen from both ends.

    **`*Asterisk*` presets are only hidden inside a vendor bundle.** The
    convention is not a naming rule the UI applies everywhere -- it marks a
    non-instantiable *system* preset. Dropped into <datadir>/print/ as ordinary
    user presets they are just presets whose names contain asterisks, so all six
    bases showed up in the dropdown next to the 19 real ones.

    **A user preset may only inherit from a SYSTEM preset.** Our concrete presets
    are five-line stubs -- `inherits`, layer height, z-hop, compatible_printers,
    default_speed -- and everything else comes from the base. As loose user
    presets that inheritance had no legal parent to resolve against, so the
    stubs were nearly empty and the bases had to be visible for the values to
    exist at all. Hiding the bases without moving to a vendor bundle would have
    produced 19 blank profiles.

    A vendor bundle fixes both at once: the bases become real hidden parents, the
    stubs resolve against them, and the presets present as system profiles.

    Needs three files in agreement -- <datadir>/vendor/SVZero.ini, the matching
    .idx (PrusaSlicer will not load a vendor without one), and a [vendor:SVZero]
    section in PrusaSlicer.ini naming the model and its enabled variants.
    """
    ss, _ = strip_hidden(model, ss)
    os.makedirs(out_dir, exist_ok=True)
    # machine.nozzles, not layer_heights.keys() -- layer_heights carries "_note"
    # and friends, which would be emitted as a nozzle variant named _note.
    nozzles = list(model["machine"]["nozzles"])

    out = collections.OrderedDict()
    out["vendor"] = collections.OrderedDict([
        # PrusaSlicer 2.9's preset-repository system. Every vendor shipped with
        # 2.9.6 declares one -- "prusa-fff" for PrusaResearch, "non-prusa-fff"
        # for the third-party bundles. Without it the bundle still parses and PS
        # even logs the vendor by name and version, but it is not attached to a
        # repository, so [vendor:SVZero] is dropped from PrusaSlicer.ini on the
        # next write and the printer is never offered.
        ("repo_id", "non-prusa-fff"),
        ("name", "SV Zero (community)"),
        ("config_version", PS_CONFIG_VERSION),
        ("config_update_url", ""),
    ])
    out["printer_model:%s" % PS_PRINTER_MODEL] = collections.OrderedDict([
        ("name", "SV Zero"),
        ("variants", "; ".join(nozzles)),
        ("technology", "FFF"),
        ("family", "SV Zero"),
        ("bed_model", ""), ("bed_texture", ""), ("default_materials", ""),
    ])

    # printer_model / printer_variant are what tie a printer preset to the
    # [printer_model:] block above. Without them the vendor loads but the wizard
    # has no machine to offer, which is the PrusaSlicer analogue of the Orca
    # "vendor parses, Default Printer selected" symptom.
    resolved, _dropped = ps_bundle_resolve(ss)
    for name, kv in resolved.items():
        if ":" not in name:
            continue
        kind, preset = name.split(":", 1)
        if kind not in ("print", "filament", "printer"):
            continue          # physical_printer is per-user, never vendor data
        kv = collections.OrderedDict(kv)
        if kind == "printer" and not preset.startswith("*"):
            kv["printer_model"] = PS_PRINTER_MODEL
            noz = next((n for n in nozzles if preset.endswith("%sn" % n)), None)
            if noz:
                kv["printer_variant"] = noz
                # Every one of PrusaResearch's 146 printer presets declares this,
                # and without it PrusaSlicer loads the bundle, reports the vendor
                # by name and version, and then drops [vendor:SVZero] from
                # PrusaSlicer.ini -- the vendor quietly uninstalls itself and no
                # printer is offered. Point at the tier the model calls default.
                # Look the name up rather than rebuilding it: layer heights are
                # not formatted uniformly across presets (0.4n uses "0.20mm",
                # 0.6n uses "0.3mm"), so reconstruction silently points at a
                # preset that does not exist.
                tier = model["tiers"].get("_default_print_tier", "Standard")
                want = re.compile(r"^print:SV Zero %sn - [\d.]+mm \(%s\)$"
                                  % (re.escape(noz), tier))
                hit = next((k.split(":", 1)[1] for k in ss if want.match(k)), None)
                if hit:
                    kv["default_print_profile"] = hit
        out[name] = kv

    write_ini(os.path.join(out_dir, PS_VENDOR_ID + ".ini"),
              out, "SV Zero vendor bundle", target=("PrusaSlicer", PS_TARGET))

    # The .idx is a version manifest. min_slic3r_version gates the whole vendor:
    # set it above the installed PrusaSlicer and the bundle is silently ignored.
    with open(os.path.join(out_dir, PS_VENDOR_ID + ".idx"), "w",
              encoding="utf-8") as fh:
        fh.write("min_slic3r_version = %s\n" % PS_MIN_VERSION)
        fh.write("%s Generated by tools/generate.py from source/model.json\n"
                 % PS_CONFIG_VERSION)

    return len(nozzles), sum(1 for k in out if k.startswith("print:"))


def emit_ps(model, ss, out_path):
    """PrusaSlicer from SuperSlicer -- a translation, not a copy.

    It was a copy until 2026-09-02, which is why the published bundle disagreed
    with the loose presets generated from the same master. See
    ps_bundle_resolve.
    """
    pub, _ = strip_hidden(model, ss)
    pub = collections.OrderedDict((k, collections.OrderedDict(v)) for k, v in pub.items())
    inject_chamber(pub, "ps")
    out, dropped = ps_bundle_resolve(pub)
    write_ini(out_path, out, "PrusaSlicer bundle, derived from the SuperSlicer master",
              target=("PrusaSlicer", PS_TARGET))
    return dropped


# SuperSlicer expresses most speeds as a percentage of another speed, and the
# reference differs per key. Orca wants absolute mm/s. A rename alone therefore
# writes "83%" into an Orca field that means mm/s -- name-correct and
# unit-wrong, which nothing will flag.
#
# MEASURED, 2026-09-01, not documented-and-hoped. Every line below is quoted
# from `superslicer --help-fff` on the installed 2.7.62.0-beta2 binary, and the
# four that were wrong were caught first by tools/slice-harness.py: it sliced
# the test object and read the feedrates SuperSlicer actually emitted.
#
#   perimeter_speed            "over the Default speed"
#   external_perimeter_speed   "over the Internal Perimeters speed setting"
#   solid_infill_speed         "over the Default speed"          <-- was infill_speed
#   infill_speed               "over the Solid Infill speed"     <-- was default_speed
#   top_solid_infill_speed     "over the Solid Infill speed"
#   gap_fill_speed             "over the Internal Perimeters"    <-- was infill_speed
#   bridge_speed               "over the Default speed"
#   support_material_speed     "over the Default speed"
#   first_layer_infill_speed   "will scale the current infill speed"  <-- was solid
#
# The old table had infill and solid infill EXACTLY INVERTED. It survived
# because our masters set infill_speed = 100%, and 100% of default happens to
# equal what solid resolves to whenever solid is a percentage of default -- so
# solid came out right for the wrong reason and only infill was visibly wrong,
# by 47%.
#
# PrusaSlicer and SuperSlicer DISAGREE on first_layer_infill_speed: PS 2.9.6's
# own --help-fff says "a percantage of the solid infill speed", SuperSlicer says
# the infill speed. The master is SuperSlicer, so SuperSlicer's meaning is the
# one being expressed; resolving to an absolute value with the master's
# semantics is exactly what makes that meaning survive into a dialect that would
# otherwise reinterpret it.
PCT_REFERENCE = {
    "perimeter_speed": "default_speed",
    "solid_infill_speed": "default_speed",
    "bridge_speed": "default_speed",
    "support_material_speed": "default_speed",
    "external_perimeter_speed": "perimeter_speed",
    "gap_fill_speed": "perimeter_speed",
    "infill_speed": "solid_infill_speed",
    "top_solid_infill_speed": "solid_infill_speed",
    "first_layer_infill_speed": "infill_speed",
    # Added 2026-09-07. Unset, PrusaSlicer defaults small perimeters to 15 mm/s
    # ABSOLUTE against our 158.6 mm/s perimeters -- and then first_layer_speed,
    # which PrusaSlicer treats as a MULTIPLIER, stacked on top: 10.5% x 15 =
    # 1.575 mm/s = F94.5, which is what a live print was actually running at.
    # SuperSlicer and Orca both express this as a percentage of the internal
    # perimeter speed, so that is the reference.
    "small_perimeter_speed": "perimeter_speed",
}


def resolve_speeds(flat, *, round_output=True):
    """Evaluate the full speed chain before rounding derived outputs to tenths.

    Orca requests unrounded values because its first-layer approximation still
    needs to multiply them. Its emitter rounds after that final calculation.
    """
    out, pending = dict(flat), True
    computed = set()
    rounds = 0
    while pending and rounds < 6:
        pending, rounds = False, rounds + 1
        for key, ref in PCT_REFERENCE.items():
            v = out.get(key)
            if not isinstance(v, str) or not v.strip().endswith("%"):
                continue
            base = out.get(ref)
            if isinstance(base, str) and base.strip().endswith("%"):
                pending = True         # base not resolved yet, try next round
                continue
            try:
                out[key] = str(float(v.strip().rstrip("%")) / 100.0 * float(base))
                computed.add(key)
            except (TypeError, ValueError):
                pending = False        # no usable base; leave it and let --strict catch it
    if round_output:
        for key in computed:
            out[key] = computed_motion(float(out[key]))
    return out


# Orca takes these as absolute values, measured across its own profile tree
# rather than assumed. Counts are percent-vs-absolute:
#   initial_layer_print_height   0 / 1000     bridge_flow            0 / 888
#   outer_wall_acceleration      0 / 578      inner_wall_acceleration 0 / 800
#   top_surface_acceleration     0 / 757      initial_layer_speed  143 / 750
# These stay percentages, because Orca genuinely uses them that way:
#   sparse_infill_density 798/4, infill_wall_overlap 805/6,
#   retract_before_wipe 315/10, overhang_fan_threshold.
ORCA_PCT_OK = ("sparse_infill_density", "infill_wall_overlap",
               "retract_before_wipe", "overhang_fan_threshold")


def orca_percent_to_ratio(doc, keymap):
    """Orca states flow ratios as 0-2; PS/SS state them as a percentage.

    Same quantity, different scale, and Orca refuses out of range rather than
    coercing: `bottom_solid_infill_flow_ratio: 100 not in range [0,2]`. The
    upstream validator accepted the identical file -- 100 is a valid number and
    only the slicer knows the bound -- so this class is invisible to everything
    except an actual slice.
    """
    n = 0
    for key in keymap.get("_percent_to_ratio", {}).get("orca_keys", []):
        v = str(doc.get(key, "")).strip()
        if v.endswith("%"):
            try:
                doc[key] = "%g" % (float(v[:-1]) / 100.0)
                n += 1
            except ValueError:
                pass
    return n


def orca_absolutise(doc, flat, nozzle):
    """Resolve the percentages Orca will not accept, each against its own base.

    A percentage in a field Orca reads as absolute is not a rounding problem, it
    is unsliceable -- "Invalid spacing supplied to Flow::with_spacing()" came
    from initial_layer_print_height arriving as "62.5%".

    The bases are the ones SuperSlicer means: widths and layer heights are
    percentages of the NOZZLE, accelerations of default_acceleration, speeds of
    default_speed, and bridge_flow_ratio is a ratio wearing a percent sign.
    """
    def num(key, default=0.0):
        try:
            return float(str(flat.get(key, default)).strip().rstrip("%"))
        except ValueError:
            return default
    accel, speed = num("default_acceleration"), num("default_speed")
    # ORCA APPROXIMATES A CURVE IT CANNOT EXPRESS.
    #
    # first_layer_speed is a per-feature multiplier in PS/SS, so their first
    # layer is GRADED -- each feature keeps its ratio to the others. Orca has two
    # absolute fields and no multiplier, so it cannot do that. The answer is to
    # match the multiplier's OUTPUT on the features Orca's two fields actually
    # govern, not to flatten the other two down to Orca's limit: resolving
    # against default_speed would put Orca at 24.5 where PS/SS run 13.8-16.7.
    #
    # Briefly flattened all three on 2026-09-07 and reverted the same day.
    # Matching the weakest dialect is lowest-common-denominator, not consistency.
    ORCA_FIRST_LAYER = {"initial_layer_speed": num("perimeter_speed"),
                        "initial_layer_infill_speed": num("solid_infill_speed")}
    n = 0
    for k, v in list(doc.items()):
        if k in ORCA_PCT_OK or not isinstance(v, str) or not v.strip().endswith("%"):
            continue
        try:
            pct = float(v.strip()[:-1])
        except ValueError:
            continue
        if k.endswith("_acceleration") and accel:
            doc[k] = computed_motion(pct / 100.0 * accel)
        elif k == "initial_layer_print_height":
            doc[k] = "%g" % (pct / 100.0 * float(nozzle))
        elif k in ORCA_FIRST_LAYER and ORCA_FIRST_LAYER[k]:
            # Orca rejects first-layer speeds below 1 mm/s. Large-nozzle
            # Strength presets reach that floor after flow scaling.
            doc[k] = computed_motion(max(1.0, pct / 100.0 * ORCA_FIRST_LAYER[k]))
        elif k.endswith("_speed") and speed:
            doc[k] = computed_motion(pct / 100.0 * speed)
        elif k == "bridge_flow":
            doc[k] = "%g" % (pct / 100.0)
        else:
            continue
        n += 1
    return n


def widths_to_mm(doc, nozzle):
    """Orca line widths are millimetres. SuperSlicer's are percentages of nozzle
    diameter, and a rename carries the number across unchanged.

    "100%" written into a field Orca reads as mm is not merely wrong, it is
    unsliceable -- the plater fails with

        Invalid spacing supplied to Flow::with_spacing(), check your layer
        height and extrusion width

    Measured across the OrcaSlicer profile tree the convention is not ambiguous:
    8405 absolute values against 216 percentages, and every Sovol profile,
    including the Zero's own, is absolute.

    This is the same class of defect as PCT_REFERENCE further up -- name-correct
    and unit-wrong, which nothing flags until a slice is attempted.
    """
    n = 0
    for k, v in list(doc.items()):
        if not k.endswith("line_width") or not isinstance(v, str):
            continue
        t = v.strip()
        if t.endswith("%"):
            try:
                doc[k] = "%g" % (float(t[:-1]) / 100.0 * float(nozzle))
                n += 1
            except ValueError:
                pass
    # Sovol always states the base width explicitly; inheriting it from
    # fdm_process_common would silently reintroduce someone else's nozzle.
    doc.setdefault("line_width", "%g" % float(nozzle))
    return n


def emit_orca(model, ss, keymap, out_dir):
    """Orca process JSONs, translated via the keymap.

    Only mapped keys are emitted. Anything unmapped is reported rather than
    guessed — writing a plausible value into the wrong Orca setting is worse
    than omitting it, because nothing errors.
    """
    # Clear first: a renamed tier leaves a stale profile behind otherwise, and a
    # stale profile is indistinguishable from a current one once it is imported.
    os.makedirs(out_dir, exist_ok=True)
    for old in os.listdir(out_dir):
        if old.endswith(".json"):
            os.remove(os.path.join(out_dir, old))
    rev = {v: k for k, v in keymap["mapped"].items()}
    # Orca-only decisions, applied after translation. Same role as
    # filament_scope._forced: these are settings with no PS/SS key to translate
    # from, or ones whose scope differs, so nothing upstream can carry them.
    forced_process = {k: v for k, v in keymap.get("process_forced", {}).items()
                      if not k.startswith("_")}
    # ...and its counterpart: PS/SS keys withheld from Orca on purpose. See
    # keymap.json process_omit for why each one is there.
    omit_process = {k for k in keymap.get("process_omit", {})
                    if not k.startswith("_")}
    written, unmapped, unresolved = 0, collections.Counter(), []
    written_names = []
    for name, kv in ss.items():
        m = re.match(r"print:SV Zero (\d\.\d)n - ([\d.]+)mm \((\w+)\)", name)
        if not m:
            continue
        noz, lh, tier = m.group(1), m.group(2), m.group(3)
        # Flatten the inheritance chain: Orca has inheritance too, but the
        # parents differ, so resolve here and let Orca profiles be standalone.
        flat, cur, seen = collections.OrderedDict(), name, set()
        while cur and cur in ss and cur not in seen:
            seen.add(cur)
            for k, v in ss[cur].items():
                flat.setdefault(k, v)
            par = ss[cur].get("inherits", "").strip()
            cur = "print:%s" % par if par else None
        computed_speeds = {rev[key] for key in PCT_REFERENCE
                           if key in rev and str(flat.get(key, "")).strip().endswith("%")}
        flat = resolve_speeds(flat, round_output=False)
        doc = collections.OrderedDict()
        doc["type"] = "process"
        doc["name"] = "%smm %s @SV Zero %s nozzle" % (lh, tier, noz)
        doc["from"] = "User"
        doc["instantiation"] = "true"
        doc["inherits"] = "fdm_process_common"
        for k, v in flat.items():
            if k in ("inherits", "compatible_printers"):
                continue
            if k in omit_process:
                continue
            if k in rev:
                if isinstance(v, str) and v.strip().endswith("%") and k in PCT_REFERENCE:
                    unresolved.append("%s: %s = %s" % (doc["name"], k, v))
                    continue
                doc[rev[k]] = v
            else:
                unmapped[k] += 1
        doc["compatible_printers"] = ["SV Zero %s nozzle" % noz]
        # PROCESS scope, not machine. Orca defines exclude_object in 159 of its
        # own process presets and zero machine presets; putting it on the machine
        # makes Orca drop it with "contains incorrect keys" and silently disables
        # object labelling -- which PURGE_LINE needs to find the print, and which
        # Sovol's own BED_MESH_CALIBRATE ADAPTIVE=1 needs to be adaptive at all.
        doc["exclude_object"] = "1"
        # Orca splits the first layer into two absolute fields where PS/SS have
        # one multiplier. `initial_layer_speed` arrives through the keymap;
        # `initial_layer_infill_speed` cannot, because the emitter inverts
        # `mapped` and two Orca keys pointing at one PS/SS key silently drops
        # the first. Written here instead, from the same percentage.
        # Without it Orca falls back to its own 60 mm/s default and prints a
        # first layer 2.4x faster than the other two slicers.
        fls = str(flat.get("first_layer_speed", "")).strip()
        if fls.endswith("%") and not flat.get("first_layer_infill_speed", "0").strip("0. "):
            doc["initial_layer_infill_speed"] = fls
        widths_to_mm(doc, noz)
        orca_percent_to_ratio(doc, keymap)
        orca_absolutise(doc, flat, noz)
        for key in computed_speeds:
            if key in doc and not str(doc[key]).strip().endswith("%"):
                doc[key] = computed_motion(float(doc[key]))
        # NOTE: no _sqv here. It was emitted as an int and Orca rejected the whole
        # profile with "invalid json type for _sqv" -- Orca requires every value to
        # be a string or a list of strings, and one bad profile fails the ENTIRE
        # vendor, so nothing appeared. SQV is passed to START_PRINT anyway; it was
        # never a slicer field. See model.json _sqv_note.
        # Orca is strict: any non-string scalar fails the profile, and one bad
        # profile fails the whole vendor bundle silently from the user's side.
        # Orca-only decisions last, so they cannot be overwritten by a
        # translated value and so they appear on every process preset.
        doc.update(forced_process)
        # Organic supports read a separate Orca option from normal trees.
        # Keep Orca's 3 mm default on existing nozzles, but carry the larger
        # branches needed above 1 mm; the normal-tree key alone is ignored.
        doc["tree_support_branch_diameter_organic"] = "%g" % max(
            3.0, float(flat["support_tree_branch_diameter"]))
        for k, v in list(doc.items()):
            if isinstance(v, bool) or isinstance(v, (int, float)):
                doc[k] = str(v)
            elif isinstance(v, list):
                doc[k] = [str(x) for x in v]
        written_names.append(doc["name"])
        fn = os.path.join(out_dir, "%s.json" % doc["name"].replace("/", "-"))
        json.dump(doc, open(fn, "w", encoding="utf-8"), indent=2)
        written += 1
    # Orca machine profiles, carrying the host so upload works out of the box.
    hosts = model.get("hosts")
    if hosts:
        default = next((h for h in hosts["printers"] if h.get("default")), hosts["printers"][0])
        for noz in model["machine"]["nozzles"]:
            mach = collections.OrderedDict()
            mach["type"] = "machine"
            mach["name"] = "SV Zero %s nozzle" % noz
            mach["from"] = "User"
            mach["instantiation"] = "true"
            # Inherit Sovol's own shipped machine preset rather than the bare
            # common base. Sovol ships only the 0.4; the others override
            # nozzle_diameter on top of it. Without a resolvable parent Orca
            # silently drops the preset — no error, it just never appears.
            mach["inherits"] = "SOVOL ZERO 0.4 nozzle"
            # printer_model is how Orca ties a machine preset to a registered
            # entry in `models` in OrcaSlicer.conf. Without it the preset loads
            # but attaches to nothing, and Orca silently reverts the selection to
            # whatever was there before — which is exactly what it did.
            mach["printer_model"] = "SOVOL ZERO"
            mach["nozzle_diameter"] = [noz]
            mach["printer_variant"] = noz
            mach.update({key: [value] for key, value in printer_layer_limits(ss, noz).items()})
            mach["gcode_flavor"] = "klipper"
            # PINNED, not left to Orca's default, even though the default is
            # already 1. Three things have to agree here and only one of them
            # was ever written down:
            #
            #   * before_layer_change_gcode contains "G92 E0". Print.cpp:1423
            #     rejects that outright under ABSOLUTE addressing -- '"G92 E0"
            #     was found in before_layer_gcode, which is incompatible with
            #     absolute extruder addressing'.
            #   * Sovol ships use_relative_e_distances "0" on all five of its
            #     own machines, so anything inheriting from their base and not
            #     overriding this gets absolute and hits exactly that error.
            #   * gcode_label_objects is now 1 (2026-09-05), and Orca's own
            #     tooltip opens with "Relative extrusion is recommended when
            #     using label_objects".
            #
            # Relative is what we were already getting and what the rest of the
            # profile is built around; this only stops it depending on an unset
            # default lining up with our layer G-code by luck.
            mach["use_relative_e_distances"] = "1"
            # Must match the emitted process filename exactly. "%s" % 0.2 gives
            # "0.2" while the file is "0.20mm" — a dangling reference, and Orca
            # treats a machine preset pointing at a non-existent process as
            # broken rather than defaulting.
            dflt = model["tiers"].get("_default_print_tier", "Standard")
            hit = next((n for n in written_names
                        if n.endswith("%s @SV Zero %s nozzle" % (dflt, noz))), None)
            if hit:
                mach["default_print_profile"] = hit
            # Ours, not Sovol's. This pointed at "SOVOL ZERO PLA - Brass" while
            # our own filament presets existed, so Orca had no reason to open on
            # one of them.
            mach["default_filament_profile"] = ["SV Zero PLA - Brass"]
            mach["printable_area"] = ["0x0", "152.4x0", "152.4x152.4", "0x152.4"]
            mach["printable_height"] = "152.4"
            # host_type stays either way: it says which API Moonraker speaks,
            # which is true of every Zero and is not anybody's address. Only
            # print_host is personal.
            mach["host_type"] = hosts["orcaslicer_host_type"]
            if PERSONAL:
                mach["print_host"] = default["host"]
            json.dump(mach, open(os.path.join(out_dir, "%s.json" % mach["name"]), "w",
                                 encoding="utf-8"), indent=2)
            written += 1
    return written, unmapped, unresolved


# Required on every filament preset -- without it Orca reports
# "can not find filament_id for <name>" and fails the vendor. Sovol uses the same
# generic id on all nine of its Zero profiles regardless of material, so this is
# their value, known to load, rather than one invented here.
# Orca's own start G-code, inherited from Sovol, is the sequence that crashed
# Zero2 on 2026-08-17: it homes, calls START_PRINT which homes again, homes a
# third time, and only THEN heats the bed -- so calibration and meshing run cold
# -- before driving straight to an XY position with no Z clearance.

def clean_temp_lines(data):
    """Name the material and let the macro pack own the table.

    RELOCATED 2026-09-05. This used to emit a generated if/elsif ladder ending in

        SET_GCODE_VARIABLE MACRO=_BRUSH VARIABLE=clean_temp VALUE=170

    which was the single line in the whole published profile capable of turning a
    missing macro pack into an ABORTED JOB. SET_GCODE_VARIABLE is a mux command
    keyed on MACRO, so with no _BRUSH defined Klipper raises "The value '_BRUSH'
    is not valid for MACRO". Every other command we emit is an ordinary macro
    call, and an unknown macro only warns (gcode.py:305 is respond_info, not an
    exception) -- so that one line was the difference between a profile that
    degrades and a profile that fails.

    The table now lives in nozzle_brush.cfg as _BRUSH_CLEAN_TEMPS, matching what
    COOLDOWN_ARM already does one block further down: the slicer names the
    material, the pack owns the policy. One table serves every slicer, the spool
    can name the material when no slicer does, and the emitted G-code stops
    carrying a copy of a curve that is still only proposed.

    `data` is kept as the argument so source/filament-clean-temp.json remains the
    single documented home of the curve -- it is now the source for the macro's
    table rather than for these lines, and the comment points a reader at it.
    """
    return [
        ";>>>CLEAN_TEMP",
        ";  The material is NAMED here; the temperature table lives in the macro",
        ";  pack, in nozzle_brush.cfg's _BRUSH_CLEAN_TEMPS. Same division as",
        ";  COOLDOWN_ARM below. Provenance for the curve is",
        ";  source/filament-clean-temp.json.",
        ";  Safe to omit: without the pack this is an unknown command and only",
        ";  warns, and CLEAN_NOZZLE falls back to _BRUSH.retract_temp.",
        "SET_CLEAN_TEMP MATERIAL=[filament_type]",
        ";<<<CLEAN_TEMP",
    ]

def cooldown_lines(data):
    """Post-print cooldown policy, as start-G-code lines.

    ONE LINE NOW. It used to emit the rates themselves, per material, which meant
    the numbers existed in two places -- this table and the macro -- and a print
    sliced anywhere else silently took the generic row. The table moved into
    cooldown.cfg's _COOLDOWN_MATERIALS, so the slicer's whole job is to NAME the
    material and Klipper owns the policy.

    That is what makes the same print get the same descent from Orca, from
    PrusaSlicer, from Sovol's stock profile, or from no slicer involvement at all
    -- when nothing names it, END_PRINT falls back to the material spool_guard
    latched off Spoolman at print start. Same principle as END_PRINT's retract
    debt closing through CLEAN_NOZZLE with no slicer in the loop.

    Emitted into the START G-code, not the end G-code, even though END_PRINT is
    what consumes it: the policy has to be in RAM before the print finishes, and
    the start block is where the other generated material lookup already lives.

    Omitting the whole block stays supported and safe.

    `data` is still read, and still only for its comment: the rates it carries
    are no longer authoritative and the file says so.
    """
    return [
        ";>>>COOLDOWN generated from source/filament-cooldown.json -- do not hand-edit",
        ";  Names the material; the RATES live in cooldown.cfg's",
        ";  _COOLDOWN_MATERIALS, so one table serves every slicer and the spool",
        ";  can name the material when no slicer does. Pass FINAL, CHAMBER_RATE",
        ";  or BED_RATE here only to OVERRIDE the table for an experiment.",
        ";  Omitting this block is safe: END_PRINT then asks spool_guard what is",
        ";  loaded, and falls back to the default row if nothing knows.",
        "COOLDOWN_ARM MATERIAL=[filament_type]",
        ";<<<COOLDOWN",
    ]



# Sovol's start G-code does not end at the temperature wait: it continues into
# its OWN two-segment purge, wrapped in a first_layer_print_min test, and only
# then reaches SET_PRINT_STATS_INFO. Replacing just the preamble left that purge
# in place, so Orca emitted Sovol's literal purge lines AND called PURGE_LINE --
# a double purge, and the one visible in the slicer preview was Sovol's, because
# a macro cannot be previewed. Everything from the conditional to the endif goes.

ORCA_FILAMENT_ID = "GFL99"
ORCA_FILAMENT_STRUCTURAL = ("type", "filament_id", "setting_id", "name", "from",
                            "instantiation", "inherits")
ORCA_FILAMENT_PLAIN = ("compatible_printers_condition", "compatible_prints_condition")

ORCA_FILAMENT_ROOTS = ("fdm_filament_common", "fdm_filament_pla", "fdm_filament_pet",
                       "fdm_filament_abs", "fdm_filament_tpu", "fdm_filament_pc")


def emit_orca_filament(model, ss, keymap, out_dir):
    """Filament presets for Orca, which previously got none at all.

    PS/SS carried 18 filament presets and Orca carried zero: emit_orca walked
    only process and machine, and the vendor's filament_list was empty. So
    "filament does not import in Orca" was never a parse failure -- nothing was
    being written.

    Three things make this harder than a rename, and all three have already
    failed the whole vendor once in this project's history:

    * **A single bed temperature becomes four.** Orca carries one per plate type
      rather than one per preset. Ours is written to all four, so whichever plate
      the user selects gets the intended temperature.
    * **pressure_advance is ignored without enable_pressure_advance.**
    * **The inheritance closure must be shipped AND listed.** Each preset
      inherits a per-material root, each root inherits fdm_filament_common, and
      an unlisted root is an absent root. PETG's is fdm_filament_pet, not _petg.

    activate_air_filtration is forced to 0. That is a decision, not a
    translation: it drives the exhaust fan at a fixed duty for the entire print,
    and on this machine the chamber PI loop owns that fan.
    """
    fs = keymap["filament_scope"]
    simple, fan_out = fs["simple"], fs["_fan_out"]
    paired, forced, roots = fs["_paired"], fs["_forced"], fs["_roots"]
    os.makedirs(out_dir, exist_ok=True)
    # CLEAR FIRST. Every other emitter does; this one did not, and the (8M)
    # merge on 2026-09-06 left nine deleted filaments on disk -- which
    # emit_orca_vendor then packaged and INDEXED, so the Orca vendor offered
    # eighteen filaments where the master has nine. The upstream validator
    # passed it: an orphan is a perfectly valid profile, just not one anybody
    # asked for. Same failure the tier rename caused in emit_ps_presets.
    for old_file in os.listdir(out_dir):
        if old_file.endswith(".json"):
            os.remove(os.path.join(out_dir, old_file))
    machines = ["SV Zero %s nozzle" % n for n in model["machine"]["nozzles"]]
    written, unmapped = [], collections.Counter()

    for name in ss:
        if not name.startswith("filament:"):
            continue
        preset = name.split(":", 1)[1]
        if preset.startswith("*"):
            continue                      # bases are flattened away, as for PS
        kv = flatten(ss, name)
        doc = collections.OrderedDict()
        doc["type"] = "filament"
        doc["filament_id"] = ORCA_FILAMENT_ID
        doc["name"] = preset
        doc["from"] = "system"
        doc["instantiation"] = "true"

        ftype = kv.get("filament_type", "PLA")
        doc["inherits"] = roots.get(ftype, roots["_base"])

        for k, v in kv.items():
            if k in ("inherits", "compatible_printers"):
                continue
            if k in simple:
                doc[simple[k]] = v
            elif k in fan_out:
                for ok in fan_out[k]:
                    doc[ok] = v
            elif k in paired:
                doc[paired[k]["key"]] = v
                doc.update(paired[k]["also"])
            elif not k.startswith("_"):
                unmapped[k] += 1
        doc.update(forced)
        doc.pop("_why", None)
        # The PLA-only chamber override that used to sit here is GONE. It existed
        # because activate_chamber_temp_control was forced off for everything,
        # which left PLA as the single regulated material and every other one on
        # the macro's bare-M191 fallback of 22 C minimum and 32 C target -- so
        # ABS carried a 60 C ceiling in its preset that never reached the
        # printer. Activation and the minimum are per-material data in
        # presets.json now, and PLA's minimum is 25 rather than its own 33 C
        # target: the old derivation made PLA wait for the ceiling it was
        # supposed to be capped at.
        doc["compatible_printers"] = machines

        # NEVER emit an empty compatibility value. compatible_prints = [""] does
        # not mean "any process", it means "only a process whose name is the
        # empty string", so every one of our filaments was incompatible with
        # every one of our processes and Orca filed the lot under "Unsupported".
        # Sovol's own filaments declare compatible_printers and nothing else.
        for k in ("compatible_prints", "compatible_prints_condition",
                  "compatible_printers_condition"):
            v = doc.get(k)
            if v in ("", [], [""], None):
                doc.pop(k, None)

        # FILAMENT PRESETS INVERT THE PROCESS-PRESET RULE. Process presets take
        # plain strings; filament settings are per-extruder, so Orca reads them
        # as arrays and indexes element 0. Passing a bare string produces
        #   [json.exception.type_error.305] cannot use operator[] with a
        #   numeric argument with string
        # and drops the preset. Counted across every filament profile in the
        # OrcaSlicer tree, the split is unambiguous: nozzle_temperature is a list
        # in 848 files against 46, filament_max_volumetric_speed 1411 against 41,
        # compatible_printers 1605 against 0. The only two settings that stay
        # bare are the *_condition expressions, at 0 lists against 52 strings.
        for k, v in list(doc.items()):
            if k in ORCA_FILAMENT_STRUCTURAL or k in ORCA_FILAMENT_PLAIN:
                doc[k] = str(v)
            elif isinstance(v, list):
                doc[k] = [str(x) for x in v]
            else:
                doc[k] = [str(v)]

        fn = "%s.json" % preset
        json.dump(doc, open(os.path.join(out_dir, fn), "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)
        written.append(preset)
    return written, unmapped


def emit_orca_vendor(model, ss, out_dir):
    """Ship Orca profiles as a VENDOR BUNDLE, not loose user presets.

    Loose user presets could not work, and the reason is worth recording. Orca
    validates a machine preset against a registered `machine_model`, and Sovol's
    SOVOL ZERO model declares `"nozzle_diameter": "0.4"` — only 0.4. Presets for
    0.2/0.6/0.8/1.0 therefore describe variants their model does not admit, and
    Orca drops them without a word. Naming ours `SV Zero …` under a model called
    `SOVOL ZERO` compounded it.

    A vendor bundle lets us declare our own model with all five variants, which
    is exactly what Sovol's own bundle and jbob-afaik's fork do. Machine presets
    are built from Sovol's shipped 0.4 preset so the bundle is self-contained and
    does not require the Sovol vendor to be installed alongside.
    """
    src_machine = os.path.join(SOVOL_VENDOR, "Sovol", "machine",
                               "SOVOL ZERO 0.4 nozzle.json")
    base = json.load(open(src_machine, encoding="utf-8"))
    hosts = model.get("hosts", {})
    default_host = next((h for h in hosts.get("printers", []) if h.get("default")),
                        {"host": ""})
    nozzles = model["machine"]["nozzles"]

    vend = os.path.join(out_dir, "SVZero")
    for sub in ("machine", "process", "filament"):
        os.makedirs(os.path.join(vend, sub), exist_ok=True)

    # CLEAR EVERY SUBDIR BEFORE WRITING ANYTHING, for the reason emit_orca
    # clears its own output: a renamed tier leaves its old profile behind, and
    # Orca resolves `inherits` against the index rather than the disk, so an
    # orphan is invisible to the slicer and indistinguishable from a current
    # profile to a human reading the repo. Found 2026-09-05 with 19 orphans
    # against 20 indexed -- old tier names from before a rename.
    #
    # ORDER IS THE WHOLE POINT, and getting it wrong cost a working bundle.
    # Introduced on 2026-09-05, this loop originally ran AFTER the machine_model
    # below was written, so it deleted the model it had just created. Nothing
    # rewrote it, the index kept pointing at machine/SV Zero.json, and Orca
    # failed the ENTIRE vendor on the one unresolvable entry -- no Zero in the
    # printer list and no filaments at all, with the only clue in Orca's own log
    # ("unexpected end of input"). Every write in this function must come after
    # this loop.
    for sub in ("process", "machine", "filament"):
        d = os.path.join(vend, sub)
        if os.path.isdir(d):
            for old_file in os.listdir(d):
                if old_file.endswith(".json"):
                    os.remove(os.path.join(d, old_file))

    # the machine_model — this is the piece that was missing
    mm = collections.OrderedDict([
        ("type", "machine_model"), ("name", "SV Zero"), ("model_id", "SV-ZERO"),
        ("nozzle_diameter", ";".join(nozzles)), ("machine_tech", "FFF"),
        ("family", "SOVOL"),
        ("bed_model", base.get("bed_model", "")),
        ("bed_texture", base.get("bed_texture", "")),
        ("default_materials", base.get("default_materials", "")),
    ])
    json.dump(mm, open(os.path.join(vend, "machine", "SV Zero.json"), "w",
                       encoding="utf-8"), indent=2)

    # Each Orca vendor must supply its own inheritance roots. Ours inherit
    # fdm_process_common / fdm_machine_common, and a vendor that does not define
    # them fails with "can not find inherits fdm_process_common" -- which, like
    # every other vendor error, kills the WHOLE bundle rather than one preset.
    #
    # Copying the file in is only half of it: Orca resolves `inherits` against
    # the presets REGISTERED IN THE VENDOR INDEX, not against what happens to be
    # on disk. An unlisted root is an absent root. Sovol's own bundle lists both,
    # which is what tipped this off -- fdm_process_common is the first entry in
    # its process_list. So the roots are seeded into the lists here, ahead of
    # everything that inherits from them.
    # Ship the assets beside the profiles. Unlike PrusaSlicer, where a user
    # preset cannot resolve a bare filename and install-presets.py has to write
    # an absolute path, an Orca VENDOR bundle resolves against its own
    # directory -- so a downloader gets the plate too, not just the sandbox.
    # The credits file travels with the assets. CC BY-SA attribution has to
    # accompany the work, and a downloader who unpacks only the Orca vendor
    # directory must still find out whose model it is.
    for asset in ("svzero_bed.stl", "svzero_bed.svg", "svzero_bed.CREDITS.md"):
        src = os.path.join(ROOT, "assets", asset)
        if os.path.exists(src):
            shutil.copy2(src, os.path.join(vend, asset))

    machine_list, process_list, filament_list = [], [], []
    roots = [("fdm_process_common", "process", process_list),
             ("fdm_machine_common", "machine", machine_list)]
    roots += [(r, "filament", filament_list) for r in ORCA_FILAMENT_ROOTS]
    for root, sub, lst in roots:
        src = os.path.join(SOVOL_VENDOR, "Sovol", sub, root + ".json")
        if os.path.exists(src):
            dst = os.path.join(vend, sub, root + ".json")
            shutil.copy2(src, dst)
            if root == "fdm_machine_common":
                # The Zero has one extruder and no filament-changing hardware.
                # Sovol's inherited "1" exposes multiple filament slots and can
                # make an unrelated slot's chamber settings control the print.
                with open(dst, encoding="utf-8") as handle:
                    common = handle.read()
                common, changed = re.subn(
                    r'("single_extruder_multi_material"\s*:\s*)"[^"]*"',
                    r'\g<1>"0"', common, count=1)
                if changed != 1:
                    raise SystemExit("single-material setting absent from Orca machine root")
                with open(dst, "w", encoding="utf-8") as handle:
                    handle.write(common)
            lst.append({"name": root, "sub_path": "%s/%s.json" % (sub, root)})

    fsrc = os.path.join(ROOT, "bundles", "orca-filament")
    for f in sorted(os.listdir(fsrc)) if os.path.isdir(fsrc) else []:
        if not f.endswith(".json"):
            continue
        doc = json.load(open(os.path.join(fsrc, f), encoding="utf-8"))
        json.dump(doc, open(os.path.join(vend, "filament", f), "w", encoding="utf-8"),
                  indent=2, ensure_ascii=False)
        filament_list.append({"name": doc["name"], "sub_path": "filament/" + f})

    for noz in nozzles:
        m = collections.OrderedDict(base)
        m["type"] = "machine"
        m["name"] = "SV Zero %s nozzle" % noz
        m["from"] = "system"
        m["instantiation"] = "true"
        # NOT "" -- Orca treats an empty inherits on a system preset as a
        # dangling parent ("can not find inherits  for SV Zero 0.2 nozzle",
        # with the empty name showing as a double space) and fails the vendor.
        # Sovol's machines inherit fdm_machine_common; ours do the same.
        m["inherits"] = "fdm_machine_common"
        m["printer_model"] = "SV Zero"
        m["printer_variant"] = noz
        m["nozzle_diameter"] = [noz]
        m.update({key: [value] for key, value in printer_layer_limits(ss, noz).items()})
        # z-hop is machine scope in Orca and printer scope in PS/SS, so it is
        # authored once on the printer variant and copied here. Without this the
        # machines carried Sovol's 0.4 for every nozzle, including the 1.0 where
        # the printer preset asks for 1.0.
        lift = ss.get("printer:SV Zero %sn" % noz, {}).get("retract_lift")
        if lift:
            m["z_hop"] = [str(lift)]
        # Repeat this on each concrete preset as well as the inherited root.
        # Explicit --load-settings slicing does not resolve the vendor root,
        # and the one-filament constraint must survive that supported path.
        m["single_extruder_multi_material"] = "0"
        # Same pin as the user-preset emitter above, and it has to be repeated
        # because the vendor machines are built here from scratch rather than
        # copied from bundles/orca -- the vendor is what Orca actually loads for
        # the printer list, so a pin that lands only in the user presets is a
        # pin that never reaches the slicer. See the long note at
        # mach["use_relative_e_distances"].
        m["use_relative_e_distances"] = "1"
        g = m.get("machine_start_gcode", "")
        g = g[0] if isinstance(g, list) else g
        if ORCA_START_CUT in g:
            tail = g.split(ORCA_START_CUT, 1)[1]
            if ORCA_PURGE_START in tail and ORCA_PURGE_END in tail:
                after = tail.split(ORCA_PURGE_START, 1)[1]
                tail = "\n" + after.split(ORCA_PURGE_END, 1)[1].lstrip("\n")
            preamble = ORCA_START_PREAMBLE.replace(
                "@@CLEAN_TEMP@@", "\n".join(clean_temp_lines(load("filament-clean-temp.json")))).replace(
                "@@COOLDOWN@@", "\n".join(cooldown_lines(load("filament-cooldown.json"))))
            m["machine_start_gcode"] = preamble + tail
        elif g.startswith(ORCA_START_HEADER.split("\n")[0]):
            # Re-generating our own output. Find the PURGE_LINE command and
            # keep only what follows it as the tail, so the preamble is
            # replaced wholesale and changes to start_gcode.py take effect.
            purge_marker = "PURGE_LINE "
            idx = g.find(purge_marker)
            if idx >= 0:
                tail = g[g.find("\n", idx):]
            else:
                tail = "\n"
            preamble = ORCA_START_PREAMBLE.replace(
                "@@CLEAN_TEMP@@", "\n".join(clean_temp_lines(load("filament-clean-temp.json")))).replace(
                "@@COOLDOWN@@", "\n".join(cooldown_lines(load("filament-cooldown.json"))))
            m["machine_start_gcode"] = preamble + tail
        m["host_type"] = hosts.get("orcaslicer_host_type", "octoprint")
        # Same rule as the user-preset emitter: the vendor bundle is the thing
        # strangers install, so it must never carry an address.
        if PERSONAL:
            m["print_host"] = default_host["host"]
        dflt = model["tiers"].get("_default_print_tier", "Standard")
        lh = model["layer_heights"].get(noz, {}).get(dflt)
        if lh is not None:
            m["default_print_profile"] = "%smm " + dflt + " @SV Zero %s nozzle"
            m["default_print_profile"] = ("%smm %s @SV Zero %s nozzle") % (
                format(lh, ".2f"), dflt, noz)
        # THE BED MODEL AND TEXTURE. Orca resolves these as bare filenames
        # against the VENDOR directory -- the same convention Anker, Anycubic
        # and SeeMeCNC use, and SVG is accepted alongside PNG. Without them Orca
        # draws a plain rectangle, which is what both `sandbox.sh orca` and
        # `orca-shadow` were doing: shadow only rewrites machine_start_gcode, so
        # anything missing from the machine profile is missing from both.
        #
        # MODEL ONLY, NO TEXTURE. Both were set until 2026-09-07, when the
        # operator reported the texture "largely buried beneath the top surface
        # of the model". That is inherent, not a z-fight: the texture is painted
        # on the flat build rectangle at z=0, and dukinarow's model is a real
        # solid whose plate surface sits ABOVE z=0, so the model occludes it
        # everywhere the two overlap. A texture is what you show INSTEAD of a
        # model, not underneath one. The SVG still ships -- it is the fallback
        # for anyone who removes the model, and the credits cover both.
        if os.path.exists(os.path.join(ROOT, "assets", "svzero_bed.stl")):
            m["bed_custom_model"] = "svzero_bed.stl"
        fn = "SV Zero %s nozzle.json" % noz
        json.dump(m, open(os.path.join(vend, "machine", fn), "w", encoding="utf-8"), indent=2)
        machine_list.append({"name": m["name"], "sub_path": "machine/" + fn})

    for f in sorted(os.listdir(os.path.join(ROOT, "bundles", "orca"))):
        if not f.endswith(".json") or "mm" not in f:
            continue
        doc = json.load(open(os.path.join(ROOT, "bundles", "orca", f), encoding="utf-8"))
        doc["from"] = "system"
        json.dump(doc, open(os.path.join(vend, "process", f), "w", encoding="utf-8"), indent=2)
        process_list.append({"name": doc["name"], "sub_path": "process/" + f})

    idx = collections.OrderedDict([
        ("name", "SVZero"), ("version", "02.00.03.01"), ("force_update", "0"),
        ("description", "SV Zero profile pack — generated"),
        ("machine_model_list", [{"name": "SV Zero", "sub_path": "machine/SV Zero.json"}]),
        ("process_list", process_list), ("filament_list", filament_list),
        ("machine_list", machine_list),
    ])
    json.dump(idx, open(os.path.join(out_dir, "SVZero.json"), "w", encoding="utf-8"), indent=2)

    # An empty `inherits` fails the whole vendor and the only symptom is one line
    # in a log Orca writes with embedded NULs, so grep skips it as binary unless
    # forced with -a. That combination cost several cycles; fail loudly instead.
    for sub in ("process", "machine", "filament"):
        for f in sorted(os.listdir(os.path.join(vend, sub))):
            doc = json.load(open(os.path.join(vend, sub, f), encoding="utf-8"))
            if "inherits" in doc and not str(doc["inherits"]).strip():
                raise SystemExit("empty inherits in %s/%s -- Orca will reject "
                                 "the entire SVZero vendor" % (sub, f))
    return len(machine_list), len(process_list)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--emit", default="",
                    help="comma list: ss,ps,ps-presets,ps-vendor,orca,orca-vendor,ps3")
    ap.add_argument("--check", action="store_true")
    ap.add_argument("--personal", action="store_true",
                    help="bake this operator's own printer hosts into the "
                         "bundle. OFF by default: the published pack must not "
                         "carry anybody's LAN addresses, and the safe build is "
                         "the one you get by forgetting the flag.")
    args = ap.parse_args()
    # Immediately after parsing and before any emitter runs. It was set further
    # down at first, AFTER emit_physical_printers had already been called, so
    # --personal silently produced a published bundle -- the one direction of
    # that mistake that is merely useless rather than leaky, but wrong either
    # way.
    global PERSONAL
    PERSONAL = args.personal

    model = load("model.json")
    keymap = load("keymap.json")
    global PS_TARGET
    PS_TARGET = model.get("targets", {}).get("prusaslicer", {}).get("version", PS_TARGET)
    if args.emit:
        os.makedirs(os.path.join(ROOT, "bundles"), exist_ok=True)
    master = os.path.join(ROOT, "bundles", "SVZero_SuperSlicer.ini")
    presets = load("presets.json")
    schema_problems = check_fragment_schema(presets)
    for sp in schema_problems:
        print("  SCHEMA %s" % sp)
    if not schema_problems:
        print("source/presets.json conforms to PrusaSlicer 3.0 preset-schema.json")
    ss = sections_from_tree(presets, model)
    # The master carries ONE hand-written head-start line. Replace it with the
    # generated material-aware block so the temperature lives in exactly one
    # place -- source/filament-clean-temp.json -- rather than being a constant
    # hand-copied into the master, the Orca preamble and the macro.
    ct = "\\n".join(clean_temp_lines(load("filament-clean-temp.json")))
    n_ct = 0
    for sec in ss:
        g = ss[sec].get("start_gcode")
        if g and "@@CLEAN_TEMP@@" not in g:
            if ";>>>CLEAN_TEMP" in g:
                new = re.sub(r";>>>CLEAN_TEMP.*?;<<<CLEAN_TEMP", lambda m: ct, g)
            else:
                new = re.sub(r"M104 S\d+ ; head start[^\\]*", lambda m: ct, g)
            if new != g:
                ss[sec]["start_gcode"] = new
                n_ct += 1
    print("clean-temp block injected into %d start_gcode blocks" % n_ct)

    # Same treatment for the cooldown policy: one source table, injected rather
    # than hand-copied. It goes immediately after the clean-temp block so the
    # two generated material lookups stay adjacent in the emitted start G-code.
    cl = "\\n".join(cooldown_lines(load("filament-cooldown.json")))
    n_cd = 0
    for sec in ss:
        g = ss[sec].get("start_gcode")
        if not g or "@@COOLDOWN@@" in g:
            continue
        if ";>>>COOLDOWN" in g:
            new = re.sub(r";>>>COOLDOWN.*?;<<<COOLDOWN", lambda m: cl, g)
        elif ";<<<CLEAN_TEMP" in g:
            new = g.replace(";<<<CLEAN_TEMP", ";<<<CLEAN_TEMP\\n" + cl, 1)
        else:
            continue
        if new != g:
            ss[sec]["start_gcode"] = new
            n_cd += 1
    print("cooldown block injected into %d start_gcode blocks" % n_cd)

    # DERIVE FIRST, THEN CHECK. Everything below is owned by a model file and
    # is recomputed on every run; none of it is stored in source/presets.json.
    # It used to run only under `--emit ss` and be persisted into the master,
    # which made the master the carrier: `--emit ps` alone shipped whatever the
    # last `--emit ss` had baked in.
    anchors = load("tier-anchors.json")
    nkeys, skipped = emit_stock_tier(model, ss, keymap, anchors)
    print("  built *SV Zero - Stock* from anchors: %d keys mapped, %d unmapped and omitted"
          % (nkeys, len(skipped)))
    n = apply_speed_model(model, ss)
    print("  applied speed model: %d keys" % n)
    np_ = emit_physical_printers(model, ss)
    print("  emitted %d physical printers" % np_)
    nf, flowrep = apply_flow_model(model, ss)
    errs = [r for r in flowrep if r[0] == "RAMP"]
    mot = sorted({r[2] for r in flowrep if r[0] != "RAMP" and r[7] == "motion"})
    print("  applied flow model: %d preset default_speeds derived" % nf)
    if mot:
        print("     motion-limited (flow does not bind) on nozzle(s): %s"
              % ", ".join("%g" % x for x in mot))
    nsh = apply_shrinkage(ss, load("filament-shrink.json"))
    print("  applied nominal shrinkage to %d filament presets" % nsh)
    n8 = apply_8m_overrides(ss, load("filament-8m.json"))
    print("  applied SV08 MAX toolhead values to %d (8M) presets" % n8)

    problems = check(model, ss)
    problems.extend(e[1] for e in errs)
    problems.extend(schema_problems)
    print("model: %d nozzles, %d tiers (+%d variant), %d presets expected"
          % (len(model["machine"]["nozzles"]), len(model["tiers"]["_order"]),
             len(model["tiers"].get("_variants", [])),
             len(expected_presets(model))))
    for e in errs:
        print("  DRIFT  volumetric ramp: %s" % e[1])
    for p in problems:
        print("  DRIFT  %s" % p)
    if not problems:
        print("  rendered presets match the model")

    emit = {e.strip() for e in args.emit.split(",") if e.strip()}
    if problems and (args.check or emit):
        # Never overwrite a working bundle with a partial nozzle/tier tree.
        sys.exit(1)
    # Every PrusaSlicer emitter filters on this now, so it is loaded once and
    # unconditionally. It used to be loaded only for ps-presets/ps-vendor, which
    # is half of why `--emit ps` shipped keys PrusaSlicer does not define.
    _k = load("ps-known-options.json")
    PS_KNOWN.update(_k["options"]); PS_KNOWN.update(_k["metadata"])
    SS_REJECT.update(load("ss-rejected-keys.json")["keys"])
    if "ss" in emit:
        pub_ss, n_hidden = strip_hidden(model, ss)
        pub_ss = collections.OrderedDict(
            (k, collections.OrderedDict(v)) for k, v in pub_ss.items())
        inject_chamber(pub_ss, "ss")
        if n_hidden:
            print("  withheld %d hidden tier base(s) from the published bundles"
                  % n_hidden)
        write_ini(master, pub_ss, "SuperSlicer bundle — GENERATED from source/presets.json",
                  target=("SuperSlicer", model.get("targets", {}).get("superslicer", {}).get("version", "?")),
                  reject=SS_REJECT)
        print("  wrote SuperSlicer bundle")
    if "ps" in emit:
        n = emit_ps(model, ss, os.path.join(ROOT, "bundles", "SVZero_PrusaSlicer.ini"))
        print("  wrote PrusaSlicer bundle (%d keys filtered)" % n)
    if "ps-vendor" in emit:
        nm, npz = emit_ps_vendor(model, ss, os.path.join(ROOT, "bundles", "ps-vendor"))
        print("  wrote PrusaSlicer vendor bundle: %d variants, %d print presets" % (nm, npz))
    if "ss-presets" in emit:
        n = emit_ss_presets(model, ss, os.path.join(ROOT, "bundles", "ss-presets"))
        print("  wrote %d SuperSlicer per-preset .ini files" % n)
    if "ps-presets" in emit:
        pub_pp, _ = strip_hidden(model, ss)
        pub_pp = collections.OrderedDict(
            (k, collections.OrderedDict(v)) for k, v in pub_pp.items())
        inject_chamber(pub_pp, "ps")
        n = emit_ps_presets(pub_pp, os.path.join(ROOT, "bundles", "ps-presets"))
        print("  wrote %d PrusaSlicer per-preset .ini files" % n)
    if "orca" in emit:
        n, unmapped, unresolved = emit_orca(model, ss, keymap,
                                            os.path.join(ROOT, "bundles", "orca"))
        print("  wrote %d Orca process profiles" % n)
        if unresolved:
            print("  %d percentage speeds could not be resolved to mm/s and were OMITTED:" % len(unresolved))
            for u in unresolved[:8]:
                print("      %s" % u)
            problems.append("%d unresolved percentage speeds in Orca output" % len(unresolved))
        if unmapped:
            print("  %d distinct keys had no Orca mapping and were omitted:" % len(unmapped))
            for k, c in unmapped.most_common(12):
                print("      %-42s (%d presets)" % (k, c))

    if "orca-filament" in emit:
        fw, funmapped = emit_orca_filament(model, ss, keymap,
                                           os.path.join(ROOT, "bundles", "orca-filament"))
        print("  wrote %d Orca filament presets" % len(fw))
        for k, n in funmapped.most_common():
            print("     unmapped filament key: %-34s (%d presets)" % (k, n))

    # AFTER --emit orca: the vendor bundle is assembled from the process JSONs the
    # orca emitter writes, so running it first packages the previous run's files.
    if "orca-vendor" in emit:
        nm, npz = emit_orca_vendor(model, ss, os.path.join(ROOT, "bundles", "orca-vendor"))
        print("  wrote Orca vendor bundle: %d machines, %d processes" % (nm, npz))

    if args.check and problems:
        sys.exit(1)


    if "ps3" in emit:
        # PrusaSlicer 3.0. Separate module because it shares an option
        # vocabulary with PS2 and essentially nothing else -- see
        # notes/kb/prusaslicer3-profiles.md and the header of emit_ps3.py.
        import emit_ps3
        ovr = load("nozzle-overrides.json")["by_nozzle_and_tier"]
        # The per-nozzle values are exactly what forced 19 separate process
        # presets out of PS2: extrusion widths that must not inherit across
        # nozzles. In PS3 they are variants on tool.nozzle_diameter instead.
        per_nozzle = {}
        for noz, tiers in ovr.items():
            first = next(iter(tiers.values()))
            per_nozzle[noz.rstrip("n")] = {
                k: v for k, v in first.items() if k != "z_hop"}
        # The historical override table predates added nozzle sizes. Resolve
        # their widths from the current master instead of omitting the variant.
        for noz in model["machine"]["nozzles"]:
            if noz not in per_nozzle:
                flat = flatten(ss, next(name for name in ss
                    if name.startswith("print:SV Zero %sn - " % noz)))
                per_nozzle[noz] = {
                    key: "%g" % (float(str(flat[key]).lstrip("!").rstrip("%"))
                                 * float(noz) / 100)
                    if str(flat[key]).endswith("%") else flat[key]
                    for key in ovr["0.4n"][next(iter(ovr["0.4n"]))]
                    if key != "z_hop"}
        common = dict(ss.get("print:*SV Zero General*", {}))
        common.pop("inherits", None)
        # The printer preset's values come from the same PS2 printer section the
        # other emitters use, so start/end G-code and the machine limits stay in
        # one place. gcode_flavor is pinned here for the same reason it is
        # pinned in the Orca emitters: PS3's default is `reprap`, and inheriting
        # a default that happens to work is how the relative-E tangle started.
        printer_vals = {}
        for sec, kv in ss.items():
            if sec.startswith("printer:") and "SV Zero" in sec:
                printer_vals = {k: v for k, v in kv.items() if k != "inherits"}
                break
        printer_vals["gcode_flavor"] = "klipper"
        printer_vals["printer_technology"] = "FFF"
        printer_vals["use_relative_e_distances"] = 1
        out = os.path.join(ROOT, "bundles", "ps3")
        os.makedirs(out, exist_ok=True)
        nfiles, problems, dropped = emit_ps3.emit(
            model, out, PS_VENDOR_VERSION if "PS_VENDOR_VERSION" in globals()
            else "1.0.0",
            per_nozzle, common, printer_vals,
            ["1.0.0 Initial SV Zero bundle for PrusaSlicer 3"])
        for p in problems:
            print("  ps3 SCHEMA: %s" % p)
        if dropped:
            print("  ps3: %d key(s) not in PS3's schema, dropped: %s"
                  % (len(dropped), ", ".join(dropped)))
        print("  wrote PrusaSlicer 3 bundle: %d file(s), %d schema problem(s)"
              % (nfiles, len(problems)))

if __name__ == "__main__":
    main()
