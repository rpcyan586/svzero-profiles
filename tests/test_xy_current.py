#!/usr/bin/env python3
"""X/Y stepper current reaches every slicer, at each tier's value (2026-10-14).

Same carrier as the square corner velocity (tests/test_scv.py): the print
preset's Notes name it, and a start G-code block after START_PRINT hands it to
the pack's SVZERO_XY_CURRENT, which only ever lowers the configured current.
These tests read the generated bundles, not the generator, so a path that stops
carrying the value fails here. The macro itself is rendered by
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
CURRENT = GEN.tier_xy_current(MODEL)
TIER = re.compile(r"\((\w+)\)\.ini$")
MARKER = re.compile(r"SVZERO_XY_CURRENT=[0-9.]+A")
SENT = re.compile(r"SVZERO_XY_CURRENT CURRENT=([0-9.]+)")


def ini_values(path):
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string("[preset]\n" + path.read_text(encoding="utf-8"))
    return parser["preset"]


class XyCurrentTest(unittest.TestCase):
    def test_the_operator_values(self):
        # Fine to Strength at 2.4 A, Draft at printer.cfg's, Precision's from its
        # mode file (operator, 2026-10-14).
        self.assertEqual(CURRENT, {"Fine": 2.4, "Optimal": 2.4, "Standard": 2.4, "Strength": 2.4})
        self.assertEqual(MODEL["modes"]["precision"]["xy_current"], 1.4)

    def test_the_mode_file_sets_the_precision_current(self):
        text = (ROOT / "klipper/modes/svzero-mode-precision.cfg").read_text(encoding="utf-8")
        found = [float(v) for v in re.findall(r"^run_current:\s*([0-9.]+)", text, re.M)]
        self.assertEqual(found, [MODEL["modes"]["precision"]["xy_current"]] * 2)

    def test_prusaslicer_and_superslicer_presets_name_their_tier_value(self):
        for family in ("ps-presets", "ss-presets"):
            files = sorted((ROOT / "bundles" / family / "print").glob("*.ini"))
            self.assertTrue(files)
            for path in files:
                tier = TIER.search(path.name).group(1)
                with self.subTest(preset=path.name, family=family):
                    notes = ini_values(path).get("notes", "")
                    want = [GEN.xy_current_marker(CURRENT[tier])] if tier in CURRENT else []
                    self.assertEqual(MARKER.findall(notes), want)
                    # One line: the start G-code's `.*` would stop at a newline.
                    self.assertNotIn("\\n", notes)

    def test_printer_start_gcode_hands_it_over_after_the_scv(self):
        for family in ("ps-presets", "ss-presets"):
            files = sorted((ROOT / "bundles" / family / "printer").glob("*.ini"))
            self.assertTrue(files)
            for path in files:
                with self.subTest(printer=path.name, family=family):
                    g = ini_values(path)["start_gcode"]
                    lines = g.split("\\n")
                    at = lines.index(";<<<SCV")
                    self.assertTrue(lines[at + 1].startswith(";>>>XY_CURRENT"))
                    self.assertEqual(sorted({float(v) for v in SENT.findall(g)}), sorted(set(CURRENT.values())))

    def test_orca_processes_and_machines_carry_it(self):
        v = ROOT / "bundles/orca-vendor/SVZero"
        processes = sorted((v / "process").glob("*@SV Zero *.json"))
        self.assertTrue(processes)
        for path in processes:
            doc = json.loads(path.read_text(encoding="utf-8"))
            tier = re.search(r"mm (\w+) @", doc["name"]).group(1)
            with self.subTest(process=doc["name"]):
                want = [GEN.xy_current_marker(CURRENT[tier])] if tier in CURRENT else []
                self.assertEqual(MARKER.findall(doc.get("notes", "")), want)
        machines = sorted((v / "machine").glob("SV Zero * nozzle.json"))
        self.assertTrue(machines)
        for path in machines:
            g = json.loads(path.read_text(encoding="utf-8"))["machine_start_gcode"]
            g = g[0] if isinstance(g, list) else g
            with self.subTest(machine=path.name):
                self.assertNotIn("@@XY_CURRENT@@", g)
                lines = g.split("\n")
                at = lines.index("START_PRINT")
                self.assertTrue(lines[at + 1].startswith(";>>>XY_CURRENT"))
                self.assertEqual(sorted({float(x) for x in SENT.findall(g)}), sorted(set(CURRENT.values())))

    def test_both_firmware_bundles_ship_the_macro_and_the_restores(self):
        for name in ("svzero-1.3.7.cfg", "svzero-1.4.x.cfg"):
            text = (ROOT / "bundles/klipper" / name).read_text(encoding="utf-8")
            with self.subTest(bundle=name):
                self.assertIn("[gcode_macro SVZERO_XY_CURRENT]", text)
                # END_PRINT and CANCEL_PRINT each restore the configured current.
                self.assertEqual(text.count("SET_TMC_CURRENT STEPPER={stepper} CURRENT="), 2)


if __name__ == "__main__":
    unittest.main()
