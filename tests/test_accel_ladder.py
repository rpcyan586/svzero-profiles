#!/usr/bin/env python3
"""The 0.4 acceleration ladder: re-anchored, scaled to the shaper limits, still a ramp.

Option B of notes/acceleration-tiers-2026-10-05.md (operator, 2026-10-06), each
tier then scaled as a whole to a peak on the Zero's measured input-shaper limits
(operator, 2026-10-14). Expected values are written from the outside sources and
those peaks, not read from model.json, so a drifting model fails here:

- Fine is Prusa Core One 0.4 STRUCTURAL scaled to a 4000 peak.
- Standard is Sovol's stock Zero process, read from the vendored file, scaled to
  an 11500 peak.
- Every tier's peak, travel included, is Precision 2000, Fine 4000, Optimal
  6000, Standard 11500, Draft 40000, on every nozzle.
- Every feature rises or holds from Fine to Draft, in PrusaSlicer and Orca.
"""

import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)
ORCA = ROOT / "bundles/orca-vendor/SVZero/process"

# PrusaSlicer key -> Orca key
FEATURES = {
    "default_acceleration": "default_acceleration",
    "perimeter_acceleration": "inner_wall_acceleration",
    "external_perimeter_acceleration": "outer_wall_acceleration",
    "solid_infill_acceleration": "internal_solid_infill_acceleration",
    "infill_acceleration": "sparse_infill_acceleration",
    "top_solid_infill_acceleration": "top_surface_acceleration",
    "bridge_acceleration": "bridge_acceleration",
    "first_layer_acceleration": "initial_layer_acceleration",
}
LADDER = [("Fine", "0.10"), ("Optimal", "0.15"), ("Standard", "0.20"), ("Draft", "0.25")]
PEAKS = {"Precision": 2000, "Fine": 4000, "Optimal": 6000, "Strength": 6000, "Standard": 11500, "Draft": 40000}

CORE_ONE_STRUCTURAL = {
    "default_acceleration": 3000, "perimeter_acceleration": 2500,
    "external_perimeter_acceleration": 1500, "solid_infill_acceleration": 4000,
    "infill_acceleration": 6000, "top_solid_infill_acceleration": 2000,
    "bridge_acceleration": 1500, "first_layer_acceleration": 500,
}


def stock_values():
    """Sovol's stock Zero process, resolved the way Orca reads it."""
    st = json.loads((ROOT / "source/sovol-stock-process.json").read_text(encoding="utf-8"))
    default = float(st["default_acceleration"])
    pct = lambda v, base: float(v[:-1]) / 100 * base if v.endswith("%") else float(v)
    return {
        "default_acceleration": default,
        "perimeter_acceleration": float(st["inner_wall_acceleration"]),
        "external_perimeter_acceleration": float(st["outer_wall_acceleration"]),
        "solid_infill_acceleration": pct(st["internal_solid_infill_acceleration"], default),
        # Unset in stock: Orca's default for sparse infill is 100% of default.
        "infill_acceleration": pct(st.get("sparse_infill_acceleration", "100%"), default),
        "top_solid_infill_acceleration": float(st["top_surface_acceleration"]),
        "bridge_acceleration": pct(st["bridge_acceleration"], default),
        "first_layer_acceleration": float(st["initial_layer_acceleration"]),
    }


class AccelLadderTest(unittest.TestCase):
    def setUp(self):
        self.ps = G.parse_ini(ROOT / "bundles/SVZero_PrusaSlicer.ini")

    def ps_preset(self, tier, layer):
        return G.flatten(self.ps, "print:SV Zero 0.4n - %smm (%s)" % (layer, tier))

    def orca_preset(self, tier, layer):
        path = ORCA / ("%smm %s @SV Zero 0.4 nozzle.json" % (layer, tier))
        return json.loads(path.read_text(encoding="utf-8"))

    def test_standard_is_stock_scaled_to_the_x_limit(self):
        stock = stock_values()
        k = 11500 / max(stock.values())
        ps = self.ps_preset("Standard", "0.20")
        orca = self.orca_preset("Standard", "0.20")
        for key, value in stock.items():
            with self.subTest(key=key):
                self.assertAlmostEqual(float(ps[key]), value * k, delta=0.6)
                self.assertAlmostEqual(float(orca[FEATURES[key]]), value * k, delta=0.6)

    def test_fine_is_core_one_structural_scaled_to_4000(self):
        k = 4000 / max(CORE_ONE_STRUCTURAL.values())
        ps = self.ps_preset("Fine", "0.10")
        orca = self.orca_preset("Fine", "0.10")
        for key, value in CORE_ONE_STRUCTURAL.items():
            with self.subTest(key=key):
                if key == "first_layer_acceleration":
                    # Held at Optimal's, so the ladder does not go backwards.
                    self.assertLessEqual(float(ps[key]), value * k)
                    continue
                self.assertAlmostEqual(float(ps[key]), value * k, delta=0.6)
                self.assertAlmostEqual(float(orca[FEATURES[key]]), value * k, delta=0.6)

    def test_every_tier_peaks_at_its_ceiling_on_every_nozzle(self):
        for path in ORCA.glob("*@SV Zero *.json"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            tier = doc["name"].split("mm ", 1)[1].split(" @", 1)[0]
            values = [float(doc[k]) for k in list(FEATURES.values()) + ["travel_acceleration"]]
            with self.subTest(preset=doc["name"]):
                self.assertAlmostEqual(max(values), PEAKS[tier], delta=0.6)

    def test_every_feature_ramps_fine_to_draft(self):
        for key, orca_key in FEATURES.items():
            ps_seq = [float(self.ps_preset(t, l)[key]) for t, l in LADDER]
            orca_seq = [float(self.orca_preset(t, l)[orca_key]) for t, l in LADDER]
            with self.subTest(key=key):
                self.assertEqual(ps_seq, sorted(ps_seq))
                self.assertEqual(orca_seq, sorted(orca_seq))


if __name__ == "__main__":
    unittest.main()
