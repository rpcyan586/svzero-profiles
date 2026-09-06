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
import collections, glob, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OVR = json.load(open(os.path.join(ROOT, "source", "nozzle-overrides.json")))
# The option names PrusaSlicer actually accepts, from `prusa-slicer --help-fff`.
# Needed because the width check below is otherwise slicer-blind: see the note
# in check().
PS_OPTS = set(json.load(open(os.path.join(
    ROOT, "source", "ps-known-options.json")))["options"])


def parse(path):
    secs, cur = collections.OrderedDict(), None
    for line in open(path, encoding="utf-8"):
        m = re.match(r"^\[([^\]]+)\]\s*$", line)
        if m:
            cur = m.group(1); secs[cur] = collections.OrderedDict(); continue
        if cur is not None and "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1); secs[cur][k.strip()] = v.strip()
    return secs


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
    EXPECTED_GAPS = {"0.2n": {"Draft", "Speed", "ExtraDraft"}}   # old names kept: a stale bundle must still validate

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
    print("== orca vendor bundle ==")
    print("   %d indexed profile(s)" % len(referenced))
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
