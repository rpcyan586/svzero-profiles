#!/usr/bin/env python3
"""Unit tests for sandbox-only slicer preset transformations."""

import importlib.util
import json
import pathlib
import tempfile
import unittest


PATH = pathlib.Path(__file__).parents[1] / "tools/install-presets.py"
SPEC = importlib.util.spec_from_file_location("install_presets", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


class InstallPresetsTest(unittest.TestCase):
    def test_orca_shadow_mode_moves_bed_before_anchor_and_nozzle_after(self):
        with tempfile.TemporaryDirectory() as directory:
            machine_dir = pathlib.Path(directory) / "system/SVZero/machine"
            machine_dir.mkdir(parents=True)
            start = "header\n" + MODULE.ORCA_NORMAL_CHAMBER_START + "\nM104 S170\ntail"
            for nozzle in ("0.4", "0.6"):
                (machine_dir / f"SV Zero {nozzle}.json").write_text(
                    json.dumps({"machine_start_gcode": start}), encoding="utf-8")

            self.assertEqual(MODULE.enable_orca_chamber_shadow_test(directory), 2)
            result = json.loads(
                (machine_dir / "SV Zero 0.4.json").read_text(encoding="utf-8")
            )["machine_start_gcode"]
            self.assertLess(result.index("M140 S"), result.index("M191 S32"))
            self.assertLess(result.index("M191 S32"), result.index("M104 S170"))
            self.assertIn('filament_type[0]=="PLA"', result)
            self.assertIn("TOL=10 X145 Y145 Z32 STIR=1", result)
            self.assertNotIn("M141", result)
            self.assertIn("sandbox-only, not publication policy", result)


if __name__ == "__main__":
    unittest.main()
