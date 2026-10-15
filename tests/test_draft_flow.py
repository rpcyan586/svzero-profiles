#!/usr/bin/env python3
"""Draft's infill sits on the PLA cap, and nothing else reaches it (2.1.1).

Draft was Sovol's speedBenchy verbatim, authored against the 50 mm3/s machine
rating. With the 21 mm3/s PLA every tier is authored against, the slicer clamped
its walls and infill, and the outer wall ran as fast as the inner (measured in
OrcaSlicer 2.4.2 on the 0.4: 251 -> 248 outer, 302 -> 248 inner, 302 -> 216
infill). Draft is now derived, its infill asking exactly the cap
(flow_model.infill_demand; operator, 2026-10-15: "hitting 21mm3/s, not less"),
and speedBenchy is the hidden SpeedBenchy tier.

Demand is the slicers' own bead model: a rectangle with semicircular sides,
w h - h^2 (1 - pi/4), times the filament's flow ratio. Measured against the
G-code on 2026-10-14, it reproduces OrcaSlicer's extrusion to 0.1%.
"""

import collections
import importlib.util
import json
import math
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
GEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GEN)

MODEL = json.loads((ROOT / "source/model.json").read_text(encoding="utf-8"))
VENDOR = ROOT / "bundles/orca-vendor/SVZero"
PLA = json.loads((VENDOR / "filament/SV Zero PLA - Brass.json").read_text(encoding="utf-8"))
CAP = float(PLA["filament_max_volumetric_speed"][0])
RATIO = float(PLA["filament_flow_ratio"][0])


def demand(speed, width, layer):
    return float(speed) * (width * layer - layer * layer * (1 - math.pi / 4)) * RATIO


class DraftFlowTest(unittest.TestCase):
    def test_draft_infill_is_on_the_cap_and_the_walls_under_it(self):
        files = sorted((VENDOR / "process").glob("*mm Draft @SV Zero *.json"))
        self.assertEqual(len(files), 7)
        for path in files:
            doc = json.loads(path.read_text(encoding="utf-8"))
            lh = float(doc["layer_height"])
            with self.subTest(process=doc["name"]):
                # On the cap to within the 0.1 mm/s a speed is written to.
                infill = demand(doc["sparse_infill_speed"], float(doc["sparse_infill_line_width"]), lh)
                self.assertAlmostEqual(infill, CAP, delta=0.05)
                for speed, width in (("inner_wall_speed", "inner_wall_line_width"),
                                     ("outer_wall_speed", "outer_wall_line_width")):
                    self.assertLess(demand(doc[speed], float(doc[width]), lh), CAP)
                # The ordering the clamp destroyed: outer slower than inner.
                self.assertLess(float(doc["outer_wall_speed"]), float(doc["inner_wall_speed"]))

    def test_draft_is_still_the_fast_end(self):
        # Highest infill flow of the published tiers on every nozzle Draft ships on.
        procs = collections.defaultdict(dict)
        for path in (VENDOR / "process").glob("*@SV Zero [0-9]*.json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            m = re.match(r"([\d.]+)mm (\w+) @SV Zero ([\d.]+) nozzle", doc["name"])
            procs[m.group(3)][m.group(2)] = demand(doc["sparse_infill_speed"],
                                                   float(doc["sparse_infill_line_width"]), float(m.group(1)))
        for nozzle, tiers in procs.items():
            if "Draft" in tiers:
                with self.subTest(nozzle=nozzle):
                    self.assertEqual(max(tiers, key=tiers.get), "Draft")

    def test_speedbenchy_is_a_hidden_verbatim_reference(self):
        tier = MODEL["tiers"]["SpeedBenchy"]
        self.assertTrue(tier["hidden"])
        self.assertIn("SpeedBenchy", MODEL["tiers"]["_reference_tiers"])
        self.assertEqual(MODEL["tiers"]["Draft"]["anchor"], "derived")
        ss = collections.OrderedDict()
        GEN.emit_speedbenchy_tier(MODEL, ss, GEN.load("keymap.json"))
        sec = ss["print:*SV Zero - SpeedBenchy*"]
        # Sovol's 3MF: outer 350, inner 400, infill 500 at 40000.
        self.assertEqual((sec["external_perimeter_speed"], sec["perimeter_speed"], sec["infill_speed"]),
                         ("350", "400", "500"))
        for family in ("ps-presets", "ss-presets"):
            with self.subTest(family=family):
                self.assertFalse(list((ROOT / "bundles" / family).rglob("*SpeedBenchy*")))
        for bundle in ("SVZero_PrusaSlicer.ini", "SVZero_SuperSlicer.ini", "ps-vendor/SVZero.ini"):
            with self.subTest(bundle=bundle):
                self.assertNotIn("SpeedBenchy", (ROOT / "bundles" / bundle).read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
