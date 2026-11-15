#!/usr/bin/env python3
"""Every shipped shrinkage value, in every artefact, against the one physical figure.

source/filament-shrink.json holds PHYSICAL shrinkage: 0.3 means a cooled part
is 0.3% small, and the right compensation enlarges it by 100 / (100 - 0.3).
The slicers spell that two ways, and the pack has shipped the wrong spelling
before (100.8% for ABS in Orca and SuperSlicer, until 2026-10-13):

  PrusaSlicer 2 and 3   filament_shrinkage_compensation_xy/_z = s%
  SuperSlicer, Orca     filament_shrink (and Orca's _z) = (100 - s)%, the size
                        a 100 mm feature measures

Direction is MEASURED by `tools/slice-harness.py --shrinkage`, but only in
slicers whose command line applies it: PrusaSlicer 3 and SuperSlicer. The
PrusaSlicer 2.9.x and Orca command lines apply none. This file is what lets
those two measurements speak for them: an artefact here must carry exactly the
value its notation's measured sibling carries, so a value that slips in one
artefact cannot hide behind a slicer nobody can measure headless.
"""
import glob
import importlib.util
import json
from pathlib import Path
import sys
import unittest

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)

MATERIALS = json.loads((ROOT / "source/filament-shrink.json").read_text())["materials"]


def ini(path):
    return dict(line.split(" = ", 1) for line in Path(path).read_text().splitlines()
                if " = " in line and not line.startswith("#"))


def physical(name):
    """(xy, z) for a shipped filament, keyed as generate.apply_shrinkage keys it."""
    ftype = ini(ROOT / "bundles/ps-presets/filament" / ("%s.ini" % name))["filament_type"]
    m = MATERIALS.get("PLA Silk" if "Silk" in name else ftype) or MATERIALS[ftype]
    return float(m["xy"]), float(m["z"])


FILAMENTS = sorted(Path(p).stem for p in glob.glob(str(ROOT / "bundles/ps-presets/filament/*.ini")))


def pct(v):
    return "%g%%" % round(v, 4)


class ShrinkagePolarityTests(unittest.TestCase):
    def test_every_filament_ships_a_shrinkage_that_enlarges(self):
        # Never zero (every filament ships a class-nominal value), and a
        # physical shrinkage is positive: the compensation scales UP.
        self.assertEqual(len(FILAMENTS), 10)
        for name in FILAMENTS:
            xy, z = physical(name)
            self.assertGreater(xy, 0, name)
            self.assertGreater(z, 0, name)
            self.assertGreater(100.0 / (100.0 - xy), 1.0, name)

    def test_notation_functions(self):
        # The two converters, at a magnitude where an inversion cannot round
        # away: 5% shrinkage is a part measuring 95 mm per 100.
        self.assertEqual(G.orca_shrink(5), "95%")
        self.assertEqual(G.shrink_to_ps("95%"), "5%")
        self.assertEqual(G.shrink_to_ps(G.orca_shrink(0.3)), "0.3%")

    def test_prusaslicer_artefacts_carry_the_shrinkage_itself(self):
        ps3 = {d["name"]: d["values"] for d in yaml.safe_load_all(
            (ROOT / "bundles/ps3/SVZero/preset-filament-svzero.yaml").read_text())}
        bundles = {rel: G.parse_ini(ROOT / "bundles" / rel)
                   for rel in ("SVZero_PrusaSlicer.ini", "ps-vendor/SVZero.ini")}
        for name in FILAMENTS:
            xy, z = physical(name)
            with self.subTest(filament=name):
                flat = ini(ROOT / "bundles/ps-presets/filament" / ("%s.ini" % name))
                self.assertEqual(flat["filament_shrinkage_compensation_xy"], pct(xy))
                self.assertEqual(flat["filament_shrinkage_compensation_z"], pct(z))
                self.assertEqual(ps3[name]["filament_shrinkage_compensation_xy"], pct(xy))
                self.assertEqual(ps3[name]["filament_shrinkage_compensation_z"], pct(z))
                for rel, secs in bundles.items():
                    sec = G.flatten(secs, "filament:%s" % name)
                    self.assertEqual(sec["filament_shrinkage_compensation_xy"], pct(xy), rel)
                    self.assertEqual(sec["filament_shrinkage_compensation_z"], pct(z), rel)

    def test_measured_size_artefacts_carry_100_minus_the_shrinkage(self):
        ss = G.parse_ini(ROOT / "bundles/SVZero_SuperSlicer.ini")
        for name in FILAMENTS:
            xy, z = physical(name)
            with self.subTest(filament=name):
                self.assertEqual(ini(ROOT / "bundles/ss-presets/filament" / ("%s.ini" % name))
                                 ["filament_shrink"], pct(100 - xy))
                self.assertEqual(G.flatten(ss, "filament:%s" % name)["filament_shrink"],
                                 pct(100 - xy))
                for rel in ("orca-vendor/SVZero/filament", "orca-filament"):
                    doc = json.loads((ROOT / "bundles" / rel / ("%s.json" % name)).read_text())
                    self.assertEqual(doc["filament_shrink"], [pct(100 - xy)], rel)
                    self.assertEqual(doc["filament_shrinkage_compensation_z"], [pct(100 - z)], rel)


if __name__ == "__main__":
    unittest.main()
