#!/usr/bin/env python3
"""Offline installation contracts; uses archived Klipper dispatch, never a printer."""
import configparser
import contextlib
import importlib.util
import json
from pathlib import Path
import re
import types
import unittest

REPO = Path(__file__).resolve().parents[1]
PACK = REPO


def sections(path):
    """Read includes in order as Klipper does; no hardware is instantiated."""
    text = []
    for line in path.read_text().splitlines():
        match = re.fullmatch(r"\[include ([^]]+)\]", line.strip())
        if match:
            matches = sorted(path.parent.glob(match[1]))
            if not matches and not any(c in match[1] for c in "*?["):
                raise AssertionError("Missing include: " + match[1])
            for child in matches:
                text.extend(sections(child))
        else:
            text.append(line)
    return text


class InstallationContracts(unittest.TestCase):
    def test_base_includes_do_not_load_optional_python(self):
        extras = {p.stem for p in (PACK / "klipper").glob("*.py")}
        for version in ("1.3.7", "1.4.x"):
            with self.subTest(version=version):
                cp = configparser.RawConfigParser(strict=False)
                cp.read_string("\n".join(sections(PACK / ("svzero-%s.cfg" % version))))
                self.assertFalse(extras.intersection(s.split()[0] for s in cp.sections()))
                self.assertEqual(cp.has_section("probe_pressure"), version == "1.3.7")
                self.assertTrue(cp.has_section("gcode_macro PURGE_LINE"))
                self.assertTrue(cp.has_section("gcode_macro M191"))
                brush = cp["gcode_macro _BRUSH"]
                probe = "RUN_PROBE_PRESSURE" if version == "1.3.7" else "RUN_PROBE_VIR_CONTACT"
                self.assertEqual(brush["variable_probe"], '"' + probe + '"')
                if version == "1.4.x":
                    self.assertEqual(float(brush["variable_tap_x"]), 30)
                    self.assertEqual(float(brush["variable_tap_y"]), 30)
                    self.assertEqual(float(brush["variable_tap_clean_dx"]), 0)

    def test_python_is_an_explicit_separate_include(self):
        cp = configparser.RawConfigParser(strict=False)
        cp.read_string("\n".join(sections(PACK / "svzero-python.cfg")))
        self.assertEqual(set(cp.sections()), {"chamber_preheat", "spool_guard"})

    def test_public_profiles_wait_for_bed_and_keep_optional_purge(self):
        orca = PACK / "bundles/orca-vendor/SVZero"
        for p in (orca / "machine").glob("SV Zero * nozzle.json"):
            start = json.loads(p.read_text())["machine_start_gcode"]
            self.assertLess(start.index("M190 S"), start.index("\nSTART_PRINT\n"))
            self.assertIn("\nPURGE_LINE ", start)
            self.assertNotIn("SET_GCODE_VARIABLE MACRO=_BRUSH", start)
        for slicer in ("ps-presets", "ss-presets"):
            for p in (PACK / "bundles" / slicer / "printer").glob("*.ini"):
                cp = configparser.RawConfigParser()
                cp.read_string("[preset]\n" + p.read_text())
                start = cp["preset"]["start_gcode"].replace("\\n", "\n")
                self.assertLess(start.index("M190 S"), start.index("\nSTART_PRINT\n"))
                self.assertIn("\nPURGE_LINE ", start)
                self.assertNotIn("SET_GCODE_VARIABLE MACRO=_BRUSH", start)

    def test_public_skirt_survives_without_macro_purge(self):
        for slicer, key in (("ps-presets", "skirts"), ("ss-presets", "skirts")):
            for p in (PACK / "bundles" / slicer / "print").glob("*.ini"):
                cp = configparser.RawConfigParser()
                cp.read_string("[preset]\n" + p.read_text())
                self.assertGreaterEqual(int(cp["preset"][key]), 1)
                self.assertGreaterEqual(float(cp["preset"]["min_skirt_length"]), 8)
        for p in (PACK / "bundles/orca-vendor/SVZero/process").glob("*.json"):
            d = json.loads(p.read_text())
            if "@SV Zero " in d.get("name", ""):
                self.assertGreaterEqual(int(d["skirt_loops"]), 1)
                self.assertGreaterEqual(float(d["min_skirt_length"]), 8)


# Archived-dispatch evidence is recorded in docs/VALIDATION.md.

if __name__ == "__main__":
    unittest.main()
