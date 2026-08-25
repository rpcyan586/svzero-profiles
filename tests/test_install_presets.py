#!/usr/bin/env python3
"""Unit tests for sandbox-only slicer preset transformations."""

import importlib.util
import json
import pathlib
import sys
import tempfile
import unittest


PATH = pathlib.Path(__file__).parents[1] / "tools/install-presets.py"
SPEC = importlib.util.spec_from_file_location("install_presets", PATH)
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)

# The start G-code constants are owned by start_gcode.py, not by either tool.
sys.path.insert(0, str(PATH.parent))
import start_gcode  # noqa: E402


class InstallPresetsTest(unittest.TestCase):
    def test_generated_orca_zero_profiles_are_single_filament(self):
        machine_dir = PATH.parents[1] / "bundles/orca-vendor/SVZero/machine"
        paths = [machine_dir / "fdm_machine_common.json"]
        paths.extend(machine_dir / f"SV Zero {nozzle} nozzle.json"
                     for nozzle in ("0.2", "0.4", "0.6", "0.8", "1.0"))
        for path in paths:
            with self.subTest(path=path.name):
                profile = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(profile["single_extruder_multi_material"], "0")

    def test_generated_pla_profiles_use_exact_33_degree_chamber_control(self):
        directory = PATH.parents[1] / "bundles/orca-vendor/SVZero/filament"
        paths = sorted(directory.glob("SV Zero PLA*.json"))
        self.assertEqual(len(paths), 8)
        for path in paths:
            with self.subTest(path=path.name):
                profile = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(profile["activate_chamber_temp_control"], ["1"])
                self.assertEqual(profile["chamber_temperature"], ["33"])
                self.assertEqual(profile["chamber_minimal_temperature"], ["33"])
                self.assertNotIn("chamber_temperatures", profile)

    def test_neither_tool_keeps_its_own_copy_of_the_start_block(self):
        # generate.py emitted the block and install-presets.py searched for it,
        # each from its own literal. Adding _PREFLIGHT to one silently broke the
        # other. start_gcode.py owns the text now; assert the copies are gone
        # rather than trusting that nobody pastes one back.
        marker = "M140 S[bed_temperature_initial_layer_single]"
        for name in ("generate.py", "install-presets.py"):
            with self.subTest(tool=name):
                source = (PATH.parent / name).read_text(encoding="utf-8")
                self.assertNotIn(marker, source)
                self.assertIn("from start_gcode import", source)

    def test_shared_preamble_is_assembled_from_the_searched_block(self):
        # The text install-presets.py looks for must be, by construction, the
        # text generate.py emits.
        self.assertIn(start_gcode.ORCA_CHAMBER_BLOCK,
                      start_gcode.ORCA_START_PREAMBLE)

    def test_normal_chamber_start_matches_the_generated_bundle(self):
        # The shadow patcher locates its block by this exact text, but the text
        # is authored in generate.py. Nothing tied the two together, so adding
        # _PREFLIGHT to the preamble silently broke `sandbox.sh orca-shadow`
        # with "normal Orca chamber start block not found". Assert against a
        # real generated preset so the next drift fails here instead.
        machine_dir = PATH.parents[1] / "bundles/orca-vendor/SVZero/machine"
        paths = sorted(machine_dir.glob("SV Zero *.json"))
        self.assertTrue(paths)
        for path in paths:
            with self.subTest(path=path.name):
                start = json.loads(path.read_text(encoding="utf-8"))["machine_start_gcode"]
                if isinstance(start, list):
                    start = start[0]
                self.assertIn(MODULE.ORCA_NORMAL_CHAMBER_START, start)

    def test_both_orca_start_blocks_gate_before_any_heater(self):
        for name, block in (
            ("normal", MODULE.ORCA_NORMAL_CHAMBER_START),
            ("shadow", MODULE.ORCA_SHADOW_CHAMBER_START),
        ):
            with self.subTest(block=name):
                lines = [line for line in block.split("\n")
                         if line and not line.startswith(";")]
                self.assertEqual(lines[0], "_PREFLIGHT")
                self.assertTrue(lines[1].startswith("M140 "))

    def test_normal_orca_start_honors_activation(self):
        start = MODULE.ORCA_NORMAL_CHAMBER_START
        self.assertIn("{if activate_chamber_temp_control[0]}", start)
        self.assertIn("M191 S{chamber_temperature[0]}", start)
        self.assertIn("M191 S{chamber_minimal_temperature[0]}", start)
        self.assertIn("M141 S{chamber_temperature[0]}", start)
        self.assertTrue(start.endswith("{else}M191{endif}"))

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
            self.assertLess(result.index("M140 S"), result.index("M191 S{"))
            self.assertLess(result.index("M191 S{"), result.index("M104 S170"))
            self.assertIn("chamber_minimal_temperature[0] > 0", result)
            self.assertIn(
                "M191 S{chamber_minimal_temperature[0]} Z15 STIR=1 "
                + start_gcode.ORCA_SHADOW_BOOST,
                result,
            )
            self.assertIn("{if activate_chamber_temp_control[0]}", result)
            self.assertIn(
                "{else}M191 S{chamber_temperature[0]} Z15 STIR=1 "
                + start_gcode.ORCA_SHADOW_BOOST,
                result,
            )
            self.assertIn("M141 S{chamber_temperature[0]}", result)
            self.assertIn(
                "{else}M191 Z15 STIR=1 "
                + start_gcode.ORCA_SHADOW_BOOST
                + "{endif}",
                result,
            )
            self.assertNotIn("X145", result)
            self.assertIn("sandbox-only, not publication policy", result)

if __name__ == "__main__":
    unittest.main()
