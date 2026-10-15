#!/usr/bin/env python3
"""Values adopted from outside references on 2026-10-05, checked in the bundles.

- Every nozzle below 0.4 runs Sovol SV08's 0.2 wall speed as a cap and its
  accelerations (model.json small_nozzles), with every other speed kept in
  proportion through our own ratios, a first layer at 80% of the bore and 3
  walls.
- Internal bridges run at a percentage of bridge_speed that SuperSlicer and
  Orca read the same way, instead of the 400% that only SuperSlicer received.
- No filament slows down for layers under 8 s (Prusa's Core One PLA value).
"""

import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)
MODEL = G.load("model.json")
SMALL = MODEL["small_nozzles"]
ANCHOR = SMALL["motion_anchor"]
ORCA = ROOT / "bundles/orca-vendor/SVZero/process"


def anchored(key, tier):
    """The SV08 0.2 anchor's acceleration, scaled so its peak is the tier's."""
    peak = max(v for k, v in ANCHOR.items() if k.endswith("_acceleration"))
    return ANCHOR[key] * min(1.0, G.tier_ceiling(MODEL, tier) / peak)


class ReferenceAnchorTest(unittest.TestCase):
    def small_nozzles(self):
        found = [n for n in MODEL["machine"]["nozzles"] if float(n) < SMALL["below"]]
        self.assertEqual(found, ["0.2", "0.25", "0.3"])
        return found

    def test_small_nozzles_run_the_sv08_anchor_in_every_slicer(self):
        ps = G.parse_ini(ROOT / "bundles/SVZero_PrusaSlicer.ini")
        for noz in self.small_nozzles():
            names = [n for n in ps if n.startswith("print:SV Zero %sn - " % noz)]
            self.assertTrue(names)
            for name in names:
                flat = G.flatten(ps, name)
                with self.subTest(preset=name):
                    # A machine mode's tier keeps its own accelerations; see
                    # test_small_nozzle_precision_keeps_the_mode.
                    if not name.endswith("(Precision)"):
                        # A cap: where flow binds first (0.3 Standard) it runs slower.
                        self.assertLessEqual(float(flat["perimeter_speed"]), ANCHOR["perimeter_speed"])
                        tier = name.rsplit("(", 1)[1].rstrip(")")
                        for key in ("default_acceleration", "perimeter_acceleration",
                                    "external_perimeter_acceleration"):
                            self.assertAlmostEqual(float(flat[key]), anchored(key, tier), delta=0.051)
                    self.assertAlmostEqual(float(flat["first_layer_height"]),
                                           float(SMALL["first_layer_height"].rstrip("%")) / 100 * float(noz))
                    self.assertEqual(flat["perimeters"], str(SMALL["perimeters"]))
            for path in ORCA.glob("*@SV Zero %s nozzle.json" % noz):
                doc = json.loads(path.read_text(encoding="utf-8"))
                with self.subTest(preset=doc["name"]):
                    self.assertLessEqual(float(doc["inner_wall_speed"]), ANCHOR["perimeter_speed"])
                    tier = doc["name"].split("mm ", 1)[1].split(" @", 1)[0]
                    self.assertAlmostEqual(float(doc["default_acceleration"]), anchored("default_acceleration", tier), delta=0.051)
                    self.assertAlmostEqual(float(doc["inner_wall_acceleration"]), anchored("perimeter_acceleration", tier), delta=0.051)
                    self.assertAlmostEqual(float(doc["outer_wall_acceleration"]), anchored("external_perimeter_acceleration", tier), delta=0.051)
                    self.assertEqual(doc["wall_loops"], str(SMALL["perimeters"]))

    def test_small_nozzle_precision_keeps_the_mode(self):
        # Every precision preset on a smaller nozzle runs the 0.4's accelerations
        # and, at the same layer height, the 0.4's speeds, under the mode ceiling.
        mode = MODEL["modes"]["precision"]
        ps = G.parse_ini(ROOT / "bundles/SVZero_PrusaSlicer.ini")
        keys = ("default_acceleration", "perimeter_acceleration", "external_perimeter_acceleration",
                "infill_acceleration", "perimeter_speed", "infill_speed", "external_perimeter_speed")
        for noz in self.small_nozzles():
            self.assertIn(noz, mode["nozzles"])
            for lh in MODEL["layer_heights"][noz]["Precision"]:
                name = "print:SV Zero %sn - %.2fmm (Precision)" % (noz, lh)
                ref = "print:SV Zero 0.4n - %.2fmm (Precision)" % lh
                with self.subTest(preset=name):
                    flat, want = G.flatten(ps, name), G.flatten(ps, ref)
                    for key in keys:
                        self.assertEqual(flat[key], want[key], key)
                    self.assertLessEqual(float(flat["default_acceleration"]), mode["machine"]["max_accel"])
                    self.assertEqual(flat["compatible_printers"], mode["printer"] % noz)
            for lh in MODEL["layer_heights"][noz]["Precision"]:
                path = ORCA / ("%.2fmm Precision @%s.json" % (lh, mode["orca_machine"] % noz))
                doc = json.loads(path.read_text(encoding="utf-8"))
                with self.subTest(preset=doc["name"]):
                    self.assertLessEqual(float(doc["default_acceleration"]), mode["machine"]["max_accel"])
                    self.assertEqual(doc["wall_loops"], str(SMALL["perimeters"]))

    def test_other_nozzles_keep_the_tier_ladder(self):
        doc = json.loads((ORCA / "0.20mm Standard @SV Zero 0.4 nozzle.json").read_text(encoding="utf-8"))
        self.assertNotEqual(float(doc["default_acceleration"]), ANCHOR["default_acceleration"])

    def test_internal_bridge_is_the_same_share_of_bridge_everywhere(self):
        want = MODEL["speed_model"]["ratios_of_other"]["internal_bridge_speed"]
        ss = G.parse_ini(ROOT / "bundles/SVZero_SuperSlicer.ini")
        self.assertEqual(ss["print:*SV Zero General*"]["internal_bridge_speed"], want)
        share = float(want.rstrip("%")) / 100
        for path in ORCA.glob("* @SV Zero *.json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(preset=doc["name"]):
                self.assertAlmostEqual(float(doc["internal_bridge_speed"]),
                                       share * float(doc["bridge_speed"]), delta=0.11)

    def test_no_filament_slows_for_layers_under_8_seconds(self):
        for bundle in ("SVZero_PrusaSlicer.ini", "SVZero_SuperSlicer.ini"):
            ini = G.parse_ini(ROOT / "bundles" / bundle)
            for name in ini:
                if not name.startswith("filament:") or name.startswith("filament:*"):
                    continue
                value = G.flatten(ini, name).get("slowdown_below_layer_time")
                with self.subTest(bundle=bundle, filament=name):
                    self.assertIsNotNone(value)
                    self.assertGreaterEqual(float(value), 8)


if __name__ == "__main__":
    unittest.main()
