#!/usr/bin/env python3
"""Z stepper current reaches every slicer, at each tier's value (2026-11-08).

Carried exactly like X/Y (tests/test_xy_current.py): the print preset's Notes
name it, and a start G-code block after the X/Y one hands it to the pack's
SVZERO_Z_CURRENT, which only ever lowers the configured current. These tests
read the generated bundles. The macro itself is rendered by
tools/check-macros.py.
"""

import configparser
import importlib.util
import json
import pathlib
import re
import unittest

ROOT = pathlib.Path(__file__).parents[1]
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
GEN = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GEN)

MODEL = json.loads((ROOT / "source/model.json").read_text(encoding="utf-8"))
CURRENT = GEN.tier_current(MODEL, "Z")
TIER = re.compile(r"\((\w+)\)\.ini$")
MARKER = re.compile(r"SVZERO_Z_CURRENT=[0-9.]+A")
SENT = re.compile(r"SVZERO_Z_CURRENT CURRENT=([0-9.]+)")


def ini_values(path):
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string("[preset]\n" + path.read_text(encoding="utf-8"))
    return parser["preset"]


class ZCurrentTest(unittest.TestCase):
    def test_the_operator_values(self):
        # Operator, 2026-11-08. Every value is under the 1.331 A the stock
        # TMC2209 can deliver, so the macro's lower-only rule passes them all.
        self.assertEqual(CURRENT, {"Fine": 1.0, "Optimal": 1.0, "Standard": 1.0, "Strength": 1.0,
                                   "Draft": 1.2, "Precision": 0.7})

    def test_prusaslicer_and_superslicer_presets_name_their_tier_value(self):
        for family in ("ps-presets", "ss-presets"):
            files = sorted((ROOT / "bundles" / family / "print").glob("*.ini"))
            self.assertTrue(files)
            for path in files:
                tier = TIER.search(path.name).group(1)
                with self.subTest(preset=path.name, family=family):
                    notes = ini_values(path).get("notes", "")
                    want = [GEN.current_marker("Z", CURRENT[tier])] if tier in CURRENT else []
                    self.assertEqual(MARKER.findall(notes), want)
                    self.assertNotIn("\\n", notes)

    def test_printer_start_gcode_hands_it_over_after_the_xy_block(self):
        for family in ("ps-presets", "ss-presets"):
            files = sorted((ROOT / "bundles" / family / "printer").glob("*.ini"))
            self.assertTrue(files)
            for path in files:
                with self.subTest(printer=path.name, family=family):
                    g = ini_values(path)["start_gcode"]
                    lines = g.split("\\n")
                    at = lines.index(";<<<XY_CURRENT")
                    self.assertTrue(lines[at + 1].startswith(";>>>Z_CURRENT"))
                    self.assertEqual(sorted({float(v) for v in SENT.findall(g)}), sorted(set(CURRENT.values())))

    def test_orca_processes_and_machines_carry_it(self):
        v = ROOT / "bundles/orca-vendor/SVZero"
        processes = sorted((v / "process").glob("*@SV Zero *.json"))
        self.assertTrue(processes)
        for path in processes:
            doc = json.loads(path.read_text(encoding="utf-8"))
            tier = re.search(r"mm (\w+) @", doc["name"]).group(1)
            with self.subTest(process=doc["name"]):
                want = [GEN.current_marker("Z", CURRENT[tier])] if tier in CURRENT else []
                self.assertEqual(MARKER.findall(doc.get("notes", "")), want)
        machines = sorted((v / "machine").glob("SV Zero * nozzle.json"))
        self.assertTrue(machines)
        for path in machines:
            g = json.loads(path.read_text(encoding="utf-8"))["machine_start_gcode"]
            g = g[0] if isinstance(g, list) else g
            with self.subTest(machine=path.name):
                self.assertNotIn("@@Z_CURRENT@@", g)
                lines = g.split("\n")
                at = lines.index(";<<<XY_CURRENT")
                self.assertTrue(lines[at + 1].startswith(";>>>Z_CURRENT"))
                self.assertEqual(sorted({float(x) for x in SENT.findall(g)}), sorted(set(CURRENT.values())))

    def test_both_firmware_bundles_ship_the_macro_and_restore_z(self):
        for name in ("svzero-1.3.7.cfg", "svzero-1.4.x.cfg"):
            text = (ROOT / "bundles/klipper" / name).read_text(encoding="utf-8")
            with self.subTest(bundle=name):
                self.assertIn("[gcode_macro SVZERO_Z_CURRENT]", text)
                self.assertEqual(text.count('("tmc2209", "stepper_z")'), 2)


if __name__ == "__main__":
    unittest.main()
