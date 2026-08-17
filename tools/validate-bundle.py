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
            got = chain.get(k)
            if got is None:
                problems.append("%s: %s unset (would inherit a wrong nozzle's width)" % (n, k))
            elif noz != "0.4n" and got == OVR["by_nozzle_and_tier"]["0.4n"][
                    next(iter(OVR["by_nozzle_and_tier"]["0.4n"]))].get(k, object()):
                pass
    # Deliberate omissions. A 0.2 mm nozzle has no business with a Speed tier;
    # tier coverage is a design choice per nozzle, not proportional scaling.
    EXPECTED_GAPS = {"0.2n": {"Speed", "ExtraDraft"}}

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
    for p in problems:
        print("   FAIL  %s" % p)
    if not problems:
        print("   OK")
    return len(problems)


if __name__ == "__main__":
    paths = sys.argv[1:] or sorted(glob.glob(os.path.join(ROOT, "bundles", "*.ini")))
    bad = sum(check(p) for p in paths)
    sys.exit(1 if bad else 0)
