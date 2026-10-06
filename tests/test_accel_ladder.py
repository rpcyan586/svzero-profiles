#!/usr/bin/env python3
"""The 0.4 acceleration ladder: re-anchored, still a ramp, Standard still stock.

Option B of notes/acceleration-tiers-2026-10-05.md (operator, 2026-10-06).
Expected values are written from the outside sources, not read from
model.json, so a drifting model fails here:

- Fine is Prusa Core One 0.4 STRUCTURAL (PrusaResearch.ini, PrusaSlicer 2.9.6).
- Standard is Sovol's stock Zero process, read from the vendored file.
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

    def test_standard_is_stock(self):
        want = stock_values()
        ps = self.ps_preset("Standard", "0.20")
        orca = self.orca_preset("Standard", "0.20")
        for key, value in want.items():
            with self.subTest(key=key):
                self.assertAlmostEqual(float(ps[key]), value, delta=0.051)
                self.assertAlmostEqual(float(orca[FEATURES[key]]), value, delta=0.051)

    def test_fine_is_core_one_structural(self):
        ps = self.ps_preset("Fine", "0.10")
        orca = self.orca_preset("Fine", "0.10")
        for key, value in CORE_ONE_STRUCTURAL.items():
            with self.subTest(key=key):
                self.assertAlmostEqual(float(ps[key]), value, delta=0.051)
                self.assertAlmostEqual(float(orca[FEATURES[key]]), value, delta=0.051)

    def test_every_feature_ramps_fine_to_draft(self):
        for key, orca_key in FEATURES.items():
            ps_seq = [float(self.ps_preset(t, l)[key]) for t, l in LADDER]
            orca_seq = [float(self.orca_preset(t, l)[orca_key]) for t, l in LADDER]
            with self.subTest(key=key):
                self.assertEqual(ps_seq, sorted(ps_seq))
                self.assertEqual(orca_seq, sorted(orca_seq))


if __name__ == "__main__":
    unittest.main()
