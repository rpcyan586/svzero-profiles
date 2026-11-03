#!/usr/bin/env python3
"""Square corner velocity reaches every slicer, at each tier's value.

It was declared per tier in model.json for months and emitted nowhere: the
"passed to START_PRINT" path that model.json described was never built, so
every job ran printer.cfg's 5.0. Nothing failed, which is the failure. These
tests read the generated bundles, not the generator, so a path that stops
carrying the value fails here.
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
SCV = GEN.tier_scv(MODEL)
TIER = re.compile(r"\((\w+)\)\.ini$")


def ini_values(path):
    parser = configparser.RawConfigParser(strict=False, interpolation=None)
    parser.optionxform = str
    parser.read_string("[preset]\n" + path.read_text(encoding="utf-8"))
    return parser["preset"]


class ScvTest(unittest.TestCase):
    def test_every_published_tier_declares_one(self):
        for tier in MODEL["tiers"]["_order"] + ["Strength"]:
            with self.subTest(tier=tier):
                self.assertIn(tier, SCV)

    def test_fine_end_rises_to_standard_above_stock(self):
        # Until 2026-10-27 Standard was pinned to stock's SCV. It now sits above
        # it (operator), because a low SCV slows every faceted curve, not only
        # square corners. What holds is the ladder from Precision to Standard,
        # rising one step at a time, and Standard above Sovol's stock jerk.
        stock = json.loads((ROOT / "source/sovol-stock-process.json").read_text(encoding="utf-8"))
        flat = json.dumps(stock)
        jerks = {float(v) for v in re.findall(r'"[a-z_]*_jerk": ?"?([0-9.]+)', flat)}
        self.assertEqual(len(jerks), 1, jerks)
        ladder = [SCV[t] for t in ("Precision", "Fine", "Optimal", "Standard")]
        self.assertEqual(ladder, sorted(set(ladder)))
        self.assertGreater(SCV["Standard"], jerks.pop())

    def test_prusaslicer_and_superslicer_presets_name_their_tier_value(self):
        for family in ("ps-presets", "ss-presets"):
            files = sorted((ROOT / "bundles" / family / "print").glob("*.ini"))
            self.assertTrue(files)
            for path in files:
                tier = TIER.search(path.name).group(1)
                with self.subTest(preset=path.name, family=family):
                    notes = ini_values(path).get("notes", "")
                    self.assertEqual(re.findall(r"SVZERO_SCV=[0-9.]+mm", notes),
                                     [GEN.scv_marker(SCV[tier])])

    def test_printer_start_gcode_sends_it_after_start_print(self):
        for family in ("ps-presets", "ss-presets"):
            files = sorted((ROOT / "bundles" / family / "printer").glob("*.ini"))
            self.assertTrue(files)
            for path in files:
                with self.subTest(printer=path.name, family=family):
                    lines = ini_values(path)["start_gcode"].split("\\n")
                    at = lines.index("START_PRINT")
                    self.assertTrue(lines[at + 1].startswith(";>>>SCV"))
                    sent = [float(m) for line in lines
                            for m in re.findall(r"SQUARE_CORNER_VELOCITY=([0-9.]+)", line)]
                    self.assertEqual(sorted(sent), sorted(set(SCV.values())))

    def test_orca_processes_carry_it_as_jerk(self):
        files = sorted((ROOT / "bundles/orca-vendor/SVZero/process").glob("*@SV Zero *.json"))
        self.assertTrue(files)
        for path in files:
            doc = json.loads(path.read_text(encoding="utf-8"))
            tier = re.search(r"mm (\w+) @", doc["name"]).group(1)
            with self.subTest(process=doc["name"]):
                want = "%g" % SCV[tier]
                self.assertEqual({doc.get(k) for k in GEN.SCV_JERK_KEYS}, {want})
                # Orca emits nothing at all while default_jerk is 0.
                self.assertGreater(float(doc["default_jerk"]), 0)

    def test_orca_machine_ceiling_reaches_the_fastest_tier(self):
        # Orca clamps every jerk to machine_max_jerk_x; below the top tier
        # the fast tiers would silently come out at the ceiling.
        files = sorted((ROOT / "bundles/orca-vendor/SVZero/machine").glob("SV Zero *nozzle.json"))
        self.assertTrue(files)
        for path in files:
            doc = json.loads(path.read_text(encoding="utf-8"))
            with self.subTest(machine=doc["name"]):
                for key in ("machine_max_jerk_x", "machine_max_jerk_y"):
                    self.assertGreaterEqual(float(doc[key][0]), max(SCV.values()))


if __name__ == "__main__":
    unittest.main()
