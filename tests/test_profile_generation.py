#!/usr/bin/env python3
"""Regression checks for incomplete nozzle additions and generated geometry."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)


class ProfileGenerationTests(unittest.TestCase):
    def test_incomplete_nozzle_cannot_overwrite_bundles(self):
        source = {p.name: json.loads(p.read_text()) for p in (ROOT / "source").glob("*.json")}
        printers = next(f for f in source["presets.json"]["fragments"] if f["kind"] == "print")
        printers["variants"] = [v for v in printers["variants"]
                                if v.get("condition") != "tool.nozzle_diameter == 0.5"]
        with tempfile.TemporaryDirectory() as tmp:
            shutil.copytree(ROOT / "source", Path(tmp) / "source")
            bundle = Path(tmp) / "bundles/SVZero_SuperSlicer.ini"
            bundle.parent.mkdir()
            bundle.write_text("previous working bundle\n")
            with patch.object(G, "ROOT", tmp), patch.object(G, "load", source.__getitem__), \
                    patch.object(sys, "argv", ["generate.py", "--emit", "ss,ps"]), \
                    contextlib.redirect_stdout(io.StringIO()), self.assertRaises(SystemExit) as error:
                G.main()
            self.assertEqual(error.exception.code, 1)
            self.assertEqual(bundle.read_text(), "previous working bundle\n")
            self.assertEqual(list(bundle.parent.iterdir()), [bundle])

    def test_every_model_pair_has_sliceable_dimensions(self):
        model = G.load("model.json")
        vendor = ROOT / "bundles/orca-vendor/SVZero"
        processes = [json.loads(p.read_text()) for p in (vendor / "process").glob("*.json")]
        for nozzle, tier, height in G.expected_presets(model):
            with self.subTest(nozzle=nozzle, tier=tier):
                matches = [p for p in processes if p.get("name", "").endswith(
                    f" {tier} @SV Zero {nozzle} nozzle")]
                self.assertEqual(len(matches), 1)
                p = matches[0]
                self.assertEqual(float(p["layer_height"]), height)
                self.assertGreaterEqual(float(p["initial_layer_speed"]), 1.0)
                self.assertGreaterEqual(float(p["initial_layer_infill_speed"]), 1.0)
                self.assertAlmostEqual(float(p["outer_wall_line_width"]), float(nozzle))
                self.assertGreaterEqual(float(p["tree_support_tip_diameter"]), float(nozzle))
                self.assertGreaterEqual(float(p["tree_support_branch_diameter_organic"]),
                                        2 * float(nozzle))


if __name__ == "__main__":
    unittest.main()
