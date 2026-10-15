#!/usr/bin/env python3
"""Every slicer requests the accelerations the master asks for.

The master is written in SuperSlicer's dialect, where acceleration percentages
chain through a neighbour (measured from SuperSlicer 2.7.62 G-code, 0.4 Fine,
2026-10-05): outer wall from inner wall, internal infill and top from solid
infill, internal bridge from bridge, and inner wall, solid infill and bridge
from the default. The PrusaSlicer and Orca emitters used to read all of them
against the default, so outer walls ran ~2.9x and infill/top 2x higher there.

The expected values here are written from those measured relationships, not
from the generator's ACCEL_REFERENCE table, so a wrong table fails too.
"""

import importlib.util
import json
import pathlib
import unittest

ROOT = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)

# key: (Orca name, the key a percentage is measured against)
CHAIN = {
    "perimeter_acceleration": ("inner_wall_acceleration", "default_acceleration"),
    "external_perimeter_acceleration": ("outer_wall_acceleration", "perimeter_acceleration"),
    "solid_infill_acceleration": ("internal_solid_infill_acceleration", "default_acceleration"),
    "infill_acceleration": ("sparse_infill_acceleration", "solid_infill_acceleration"),
    "top_solid_infill_acceleration": ("top_surface_acceleration", "solid_infill_acceleration"),
    "bridge_acceleration": ("bridge_acceleration", "default_acceleration"),
}
ORDER = ["perimeter_acceleration", "solid_infill_acceleration", "bridge_acceleration",
         "external_perimeter_acceleration", "infill_acceleration",
         "top_solid_infill_acceleration"]


def expected(native):
    """Resolve the master's values the way SuperSlicer reads them."""
    out = {"default_acceleration": float(native["default_acceleration"])}
    for key in ORDER:
        raw = str(native[key]).strip()
        base = out[CHAIN[key][1]]
        out[key] = float(raw[:-1]) / 100 * base if raw.endswith("%") else float(raw)
    return out


class AccelChainTest(unittest.TestCase):
    def setUp(self):
        self.ss = G.parse_ini(ROOT / "bundles/SVZero_SuperSlicer.ini")
        self.ps = G.parse_ini(ROOT / "bundles/SVZero_PrusaSlicer.ini")
        self.names = [n for n in self.ss if n.startswith("print:SV Zero ")]
        self.assertTrue(self.names)

    def test_prusaslicer_requests_the_master_values(self):
        for name in self.names:
            want = expected(G.flatten(self.ss, name))
            got = G.flatten(self.ps, name)
            for key in ORDER:
                with self.subTest(preset=name, key=key):
                    self.assertAlmostEqual(float(got[key]), want[key], delta=0.051)

    def test_orca_requests_the_master_values(self):
        for name in self.names:
            want = expected(G.flatten(self.ss, name))
            noz, rest = name.split("SV Zero ", 1)[1].split("n - ", 1)
            layer, tier = rest.split("mm (")
            machine = G.printer_for(G.load("model.json"), noz, tier.rstrip(")"), orca=True)
            path = ROOT / ("bundles/orca-vendor/SVZero/process/%smm %s @%s.json"
                           % (layer, tier.rstrip(")"), machine))
            doc = json.loads(path.read_text(encoding="utf-8"))
            for key in ORDER:
                orca_key = CHAIN[key][0]
                with self.subTest(preset=doc["name"], key=orca_key):
                    self.assertAlmostEqual(float(doc[orca_key]), want[key], delta=0.051)

    def test_standard_outer_wall_is_stock_scaled(self):
        # Standard is stock scaled so its 40000 peak is 11500 (2026-10-14), so
        # its outer wall is stock's 10000 scaled the same way, through the chain.
        stock = json.loads((ROOT / "source/sovol-stock-process.json").read_text(encoding="utf-8"))
        want = float(stock["outer_wall_acceleration"]) * 11500 / float(stock["default_acceleration"])
        got = expected(G.flatten(self.ss, "print:SV Zero 0.4n - 0.20mm (Standard)"))
        self.assertLess(abs(got["external_perimeter_acceleration"] - want) / want, 0.01)


if __name__ == "__main__":
    unittest.main()
