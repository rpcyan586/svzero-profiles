#!/usr/bin/env python3
"""Structural checks on the SV Zero slicer bundles.

Catches the two failure modes this project has actually hit:

  * **Dangling `inherits`.** v1.0.3 shipped 19 print presets inheriting from
    `*SV Zero Quality*` while the sections were named `*SV Zero - Quality*`.
    The bundle looks fine until a slicer tries to resolve a parent.
  * **Silent per-nozzle drift.** The same refactor left every nozzle inheriting
    0.4 mm extrusion widths, so a 1.0 mm nozzle extruded 0.4 mm lines. Nothing
    errors; the prints are just wrong.

    ./validate-bundle.py [bundle.ini ...]      # defaults to ../bundles/*.ini
"""
import collections, glob, json, os, re, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OVR = json.load(open(os.path.join(ROOT, "source", "nozzle-overrides.json")))
# The option names PrusaSlicer actually accepts, from `prusa-slicer --help-fff`.
# Needed because the width check below is otherwise slicer-blind: see the note
# in check().
PS_OPTS = set(json.load(open(os.path.join(
    ROOT, "source", "ps-known-options.json")))["options"])
# Deliberate tier omissions, READ FROM THE MODEL rather than restated here. The
# hardcoded set below it is history: old tier names, kept so a stale bundle from
# before the rename still validates. Everything current comes from model.json,
# which is where the reason for each gap is written down.
MODEL = json.load(open(os.path.join(ROOT, "source", "model.json")))
MODEL_GAPS = {"%sn" % noz: set(t)
              for noz, t in MODEL["tier_gaps"].items()
              if not noz.startswith("_")}


def parse(path):
    secs, cur = collections.OrderedDict(), None
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^\[([^\]]+)\]\s*$", line)
        if m:
            cur = m.group(1); secs[cur] = collections.OrderedDict(); continue
        if cur is not None and "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1); secs[cur][k.strip()] = v.strip()
    return secs


def orca_upstream_validate(vendor_dir):
    """Run OrcaSlicer's OWN validator, the one its CI runs on every PR.

    Everything else here is our reading of what a slicer will accept. This is
    the loader Orca itself uses, so it answers the only question that finally
    matters: would Orca load this? Vendored at tools/vendor/ with provenance;
    skipped with a note if absent, because it is a binary and not everyone
    checking out this tree will have it.

    Found by reading .github/workflows/check_profiles.yml in the OrcaSlicer
    public upstream checkout.
    """
    exe = os.path.join(HERE, "vendor", "OrcaSlicer_profile_validator")
    if not os.path.exists(exe):
        return None, "not present -- see tools/vendor/README.md"
    try:
        p = subprocess.run([exe, "-p", vendor_dir, "-v", "SVZero", "-l", "2"],
                           capture_output=True, text=True, timeout=180)
    except (OSError, subprocess.SubprocessError) as e:
        return None, "could not run: %s" % e
    tail = [l for l in (p.stdout + p.stderr).splitlines() if l.strip()]
    return p.returncode == 0, "; ".join(tail[-2:])[:200]


def orca_orphans(vendor_dir):
    """Emitted Orca filaments must be exactly the master's filaments.

    The index is rebuilt FROM the directory, so a file left behind by a rename
    or a deletion is indexed as readily as a live one and the vendor offers it.
    That is what happened when the (8M) presets were merged away on 2026-09-06:
    nine deleted filaments stayed on disk, got packaged, got indexed, and Orca
    would have shown eighteen filaments where the master has nine. Upstream's
    validator passed it -- an orphan is a valid profile, just not one anybody
    asked for -- so this is ours to check.

    The `fdm_filament_*` roots are inheritance parents, not presets, and are
    expected on disk without a master counterpart.
    """
    fdir = os.path.join(vendor_dir, "SVZero", "filament")
    if not os.path.isdir(fdir):
        return []
    on_disk = {os.path.splitext(f)[0] for f in os.listdir(fdir)
               if f.endswith(".json") and not f.startswith("fdm_filament")}
    master = set()
    doc = json.load(open(os.path.join(ROOT, "source", "presets.json"),
                         encoding="utf-8"))
    def walk(node):
        nm = node.get("name")
        if nm and not nm.startswith("*"):
            master.add(nm)
        for c in node.get("variants", []):
            walk(c)
    for frag in doc["fragments"]:
        if frag.get("kind") == "filament":
            walk(frag)
    out = []
    for name in sorted(on_disk - master):
        out.append("orca-vendor filament %r has no master preset -- stale output"
                   % name)
    for name in sorted(master - on_disk):
        out.append("master filament %r missing from orca-vendor" % name)
    return out


def orca_json_hygiene(vendor_dir):
    """Two checks reimplemented from OrcaSlicer's scripts/orca_extra_profile_check.py.

    Reimplemented rather than invoked: that script resolves `resources/profiles`
    relative to ITSELF, so pointing it at our tree runs it against whatever
    checkout it lives in -- which is exactly what happened on the first attempt,
    validating Sovol's fork instead of ours and reporting 55 unrelated errors.

      * duplicate keys in a profile JSON. json.load keeps the last silently.
      * a profile with `instantiation: true` and no `compatible_printers`,
        which Orca offers in the UI and then cannot apply.
    """
    problems = []
    for path in sorted(glob.glob(os.path.join(vendor_dir, "**", "*.json"),
                                 recursive=True)):
        def dupes(pairs, _p=path):
            seen = {}
            for k, v in pairs:
                if k in seen:
                    problems.append("%s: duplicate key %r"
                                    % (os.path.relpath(_p, vendor_dir), k))
                seen[k] = v
            return seen
        try:
            doc = json.load(open(path, encoding="utf-8"), object_pairs_hook=dupes)
        except ValueError as e:
            problems.append("%s: not valid JSON (%s)"
                            % (os.path.relpath(path, vendor_dir), e))
            continue
        # FILAMENT PROFILES ONLY. Upstream scopes this to <vendor>/filament and
        # so must we: a machine profile IS a printer and has no business
        # declaring compatible_printers. Applied to every JSON it failed all
        # five of our machine profiles on the first run -- a rule copied
        # without its scope is a rule that invents work.
        if os.path.basename(os.path.dirname(path)) != "filament":
            continue
        if str(doc.get("instantiation", "")).lower() == "true" \
                and not doc.get("compatible_printers"):
            problems.append("%s: instantiation true with no compatible_printers"
                            % os.path.relpath(path, vendor_dir))
    return problems


def check(path):
    secs = parse(path)
    names = set(secs)
    problems = []
    # SuperSlicer's option set is a superset of PrusaSlicer's, and
    # nozzle-overrides.json is written in SuperSlicer's vocabulary. Keys
    # PrusaSlicer does not have are dropped from its bundle by generate.py --
    # correctly -- so demanding them back is asking for a key the slicer would
    # reject. Only the PrusaSlicer bundle gets this exemption; SuperSlicer is
    # still held to every key.
    is_ps = "PrusaSlicer" in os.path.basename(path)
    not_in_ps = []

    for n, s in secs.items():
        par = (s.get("inherits") or "").strip()
        if par and "%s:%s" % (n.split(":")[0], par) not in names:
            problems.append("dangling inherits: %s -> %s" % (n, par))

    # Every concrete print preset must pin the widths its nozzle needs, either
    # directly or via a parent chain that is nozzle-specific.
    for n, s in secs.items():
        m = re.match(r"print:SV Zero (\d\.\dn) - ", n)
        if not m or n.startswith("print:*"):
            continue
        noz = m.group(1)
        want = OVR["by_nozzle_and_tier"].get(noz, {})
        if not want:
            continue
        ref = next(iter(want.values()))
        chain, cur, seen = {}, n, set()
        while cur and cur in secs and cur not in seen:
            seen.add(cur)
            for k, v in secs[cur].items():
                chain.setdefault(k, v)
            par = (secs[cur].get("inherits") or "").strip()
            cur = "%s:%s" % (cur.split(":")[0], par) if par else None
        for k, v in ref.items():
            if k == "z_hop":
                continue
            if is_ps and k not in PS_OPTS:
                # Reported below rather than skipped silently: if PrusaSlicer
                # ever gains the option, this line is what says the exemption
                # is now hiding a real gap.
                not_in_ps.append(k)
                continue
            got = chain.get(k)
            if got is None:
                problems.append("%s: %s unset (would inherit a wrong nozzle's width)" % (n, k))
            elif noz != "0.4n" and got == OVR["by_nozzle_and_tier"]["0.4n"][
                    next(iter(OVR["by_nozzle_and_tier"]["0.4n"]))].get(k, object()):
                pass
    # Deliberate omissions. A 0.2 mm nozzle has no business with a Speed tier;
    # tier coverage is a design choice per nozzle, not proportional scaling.
    # A value that split across lines. The parser here reads exactly like the
    # slicer's, so a G-code line that became its own `key = value` shows up as a
    # key no slicer defines -- which is precisely how SuperSlicer reported it:
    # "Unknow setting: SET_GCODE_OFFSET Z_ADJUST (value: 0.000 MOVE=1)".
    # Multi-line values must carry a backslash-n escape; see ini_value().
    for n, sec in secs.items():
        for k in sec:
            if " " in k or k.startswith(("G0", "G1", "M1", "SET_", "; ")):
                problems.append(
                    "%s: %r parsed as a KEY -- a multi-line value was written "
                    "with a real newline instead of a \\n escape, and the "
                    "remainder became its own setting" % (n, k[:48]))

    # Annotation keys. Authored commentary belongs in the master, never in an
    # artefact -- every slicer reports them as unknown settings on load.
    for n, sec in secs.items():
        for k in sec:
            if k.startswith("_"):
                problems.append("%s: annotation key %s leaked into the artefact" % (n, k))

    # Square brackets in custom G-code are the legacy Slic3r VARIABLE syntax, and
    # both PrusaSlicer and SuperSlicer refuse to export when one does not
    # resolve:
    #
    #   G-code export failed due to invalid custom G-code sections: start_gcode
    #   Parsing error at line 32: Variable does not exist
    #
    # Caught 2026-09-06 by a headless slice, after the token had already been
    # committed and emitted into all nine artefacts. It was a COMMENT -- the word
    # skew_correction in square brackets, written in prose inside a `;` line --
    # and the templater does not care that the line is a comment. Every real
    # placeholder here is a config option name, so the option list is the oracle.
    CUSTOM_GCODE = ("start_gcode", "end_gcode", "before_layer_gcode",
                    "layer_gcode", "toolchange_gcode", "between_objects_gcode",
                    "start_filament_gcode", "end_filament_gcode",
                    "color_change_gcode", "pause_print_gcode",
                    "template_custom_gcode")
    for n, sec in secs.items():
        for key in CUSTOM_GCODE:
            for tok in sorted(set(re.findall(r"\[([A-Za-z_][A-Za-z_0-9]*)\]",
                                             sec.get(key, "")))):
                if tok not in PS_OPTS:
                    problems.append(
                        "%s: %s references [%s], which is not a slicer option -- "
                        "square brackets are the variable syntax even inside a "
                        "comment, and the export fails" % (n, key, tok))

    EXPECTED_GAPS = collections.defaultdict(set, {
        k: set(v) for k, v in MODEL_GAPS.items()})
    EXPECTED_GAPS["0.2n"] |= {"Speed", "ExtraDraft"}   # old names: a stale bundle must still validate

    tiers = sorted({m.group(1) for m in
                    (re.match(r"print:SV Zero \d\.\dn - [\d.]+mm \((\w+)\)", x) for x in names) if m})
    per_noz = collections.defaultdict(set)
    for x in names:
        m = re.match(r"print:SV Zero (\d\.\dn) - [\d.]+mm \((\w+)\)", x)
        if m:
            per_noz[m.group(1)].add(m.group(2))
    for noz, have in sorted(per_noz.items()):
        missing = set(tiers) - have - EXPECTED_GAPS.get(noz, set())
        if missing:
            problems.append("%s missing tier(s): %s" % (noz, ", ".join(sorted(missing))))

    # THE BUNDLE IS THE FORM A HUMAN IMPORTS, and it is the only artefact the
    # slice harness never executes -- there is no CLI path that loads a config
    # bundle, so it cannot be sliced the way the flattened presets can.
    #
    # What CAN be proved is equivalence. The SuperSlicer bundle keeps the
    # inheritance tree; bundles/ss-presets is the same tree flattened with no
    # translation applied. Resolve one and it must equal the other, key for key.
    # If it does, then slicing ss-presets IS a test of the bundle, and the gap
    # closes without ever loading a bundle.
    #
    # PrusaSlicer gets no such check and cannot: its per-preset files are
    # flattened AND translated -- widths to mm, speeds absolutised, pressure
    # advance rewritten as filament G-code -- so the two are meant to differ.
    if os.path.basename(path) == "SVZero_SuperSlicer.ini":
        flat_dir = os.path.join(ROOT, "bundles", "ss-presets")
        checked = mismatched = 0
        for n, sec in secs.items():
            kind, _, preset = n.partition(":")
            if kind not in ("print", "filament", "printer") or preset.startswith("*"):
                continue
            loose = os.path.join(flat_dir, kind, "%s.ini" % preset)
            if not os.path.exists(loose):
                problems.append("bundle has %s but ss-presets does not" % n)
                continue
            chain, cur, seen = {}, n, set()
            while cur and cur in secs and cur not in seen:
                seen.add(cur)
                for k, v in secs[cur].items():
                    chain.setdefault(k, v)
                par = (secs[cur].get("inherits") or "").strip()
                cur = "%s:%s" % (kind, par) if par else None
            chain.pop("inherits", None)
            want = {}
            for line in open(loose, encoding="utf-8"):
                if "=" in line and not line.lstrip().startswith("#"):
                    k, _, v = line.partition("=")
                    want[k.strip()] = v.strip()
            diff = sorted(k for k in set(chain) | set(want)
                          if chain.get(k) != want.get(k))
            checked += 1
            if diff:
                mismatched += 1
                problems.append(
                    "%s: bundle resolves differently from ss-presets on %s"
                    % (n, ", ".join(diff[:6]) + (" ..." if len(diff) > 6 else "")))
        if checked and not mismatched:
            print("   bundle == ss-presets on all %d concrete preset(s), resolved" % checked)

    print("== %s ==" % os.path.basename(path))
    print("   %d sections, %d with inherits, tiers seen: %s"
          % (len(secs), sum(1 for s in secs.values() if s.get("inherits")), ", ".join(tiers) or "—"))
    if not_in_ps:
        c = collections.Counter(not_in_ps)
        print("   note: %s not a PrusaSlicer option, exempted from the width "
              "check on %d preset(s): %s"
              % ("keys" if len(c) > 1 else "key", max(c.values()),
                 ", ".join(sorted(c))))
    for p in problems:
        print("   FAIL  %s" % p)
    if not problems:
        print("   OK")
    return len(problems)


def check_orca_vendor(vend_root):
    """Every index entry must resolve to a parseable profile, and vice versa.

    Orca resolves a vendor against the INDEX, not the disk, and fails the whole
    vendor on any single unresolvable entry -- no printers and no filaments, with
    nothing in the UI to say why. On 2026-09-05 the vendor assembler wrote the
    machine_model and then cleared the directory it had just written it into, so
    the index pointed at a file that no longer existed. The bundle looked fine in
    git (one missing file among 51) and was completely dead in the slicer.

    The reverse direction matters too: an orphan on disk is invisible to Orca and
    indistinguishable from a live profile to a human reading the repo.
    """
    idx_path = os.path.join(vend_root, "SVZero.json")
    if not os.path.exists(idx_path):
        return 0
    problems = []
    idx = json.load(open(idx_path, encoding="utf-8"))
    base = os.path.join(vend_root, "SVZero")
    referenced = set()
    for key in ("machine_model_list", "machine_list",
                "process_list", "filament_list"):
        for e in idx.get(key, []):
            sub = e.get("sub_path", "")
            referenced.add(sub)
            path = os.path.join(base, sub)
            if not os.path.exists(path):
                problems.append("%s: %s referenced but missing" % (key, sub))
            elif os.path.getsize(path) == 0:
                problems.append("%s: %s is empty" % (key, sub))
            else:
                try:
                    json.load(open(path, encoding="utf-8"))
                except Exception as exc:
                    problems.append("%s: %s unparseable (%s)"
                                    % (key, sub, type(exc).__name__))
    for sub in ("machine", "process", "filament"):
        d = os.path.join(base, sub)
        if not os.path.isdir(d):
            continue
        for f in sorted(os.listdir(d)):
            if f.endswith(".json") and "%s/%s" % (sub, f) not in referenced:
                problems.append("orphan on disk, not in the index: %s/%s"
                                % (sub, f))
    vendor_dir = os.path.join(ROOT, "bundles", "orca-vendor")
    problems += orca_json_hygiene(vendor_dir)
    problems += orca_orphans(vendor_dir)
    ok, note = orca_upstream_validate(vendor_dir)

    print("== orca vendor bundle ==")
    print("   %d indexed profile(s)" % len(referenced))
    if ok is None:
        print("   note: upstream validator %s" % note)
    elif ok:
        print("   upstream OrcaSlicer validator: PASS")
    else:
        problems.append("upstream OrcaSlicer validator rejected the bundle: %s" % note)
    for p in problems:
        print("   FAIL  %s" % p)
    if not problems:
        print("   OK")
    return len(problems)


if __name__ == "__main__":
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(ROOT, "bundles", "*.ini")))
    bad = sum(check(p) for p in paths)
    if not sys.argv[1:]:
        bad += check_orca_vendor(os.path.join(ROOT, "bundles", "orca-vendor"))
    sys.exit(1 if bad else 0)
