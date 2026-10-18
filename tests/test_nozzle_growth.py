#!/usr/bin/env python3
"""Nozzle growth compensation reaches every slicer, and ships off.

The slicer sends SVZERO_NOZZLE_GROWTH TEMP=<first-layer temperature> after
START_PRINT, with the same placeholder its own M104 uses, and before any move
or heat; START_PRINT records the reference temperature that the macro offsets
from (klipper/config/nozzle_growth.cfg). These tests read the generated
bundles, so a dialect that stops carrying the line fails here. The macro's
branches are rendered by tools/check-macros.py.
"""

import json
import pathlib
import re
import unittest

import yaml

ROOT = pathlib.Path(__file__).parents[1]
GROWTH = ROOT / "klipper/config/nozzle_growth.cfg"
START = ROOT / "klipper/config/start_print.cfg"
FAMILIES = {"SVZero_PrusaSlicer.ini", "SVZero_SuperSlicer.ini", "ps-presets",
            "ss-presets", "ps-vendor", "orca-vendor", "ps3"}


def start_gcodes():
    """{path: [start G-code, ...]} for every slicer bundle that has one."""
    found = {}
    for path in sorted((ROOT / "bundles").rglob("*")):
        if not path.is_file() or "klipper" in path.relative_to(ROOT / "bundles").parts:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        if "START_PRINT" not in text:
            continue
        if path.suffix == ".ini":
            values = [v.replace("\\n", "\n")
                      for v in re.findall(r"^start_gcode\s*=\s*(.*)$", text, re.M)]
        elif path.suffix == ".json":
            doc = json.loads(text)
            # Sovol's stock fdm_machine_common is a template every SV Zero
            # machine overrides, never a printer anyone selects.
            if doc.get("instantiation") == "false":
                continue
            g = doc.get("machine_start_gcode", "")
            values = [g[0] if isinstance(g, list) else g]
        elif path.suffix == ".yaml":
            # Parsed, not scanned: the YAML writer folds long lines.
            values = []

            def walk(node):
                if isinstance(node, dict):
                    for key, value in node.items():
                        if key == "start_gcode" and isinstance(value, str):
                            values.append(value)
                        else:
                            walk(value)
                elif isinstance(node, list):
                    for value in node:
                        walk(value)
            for doc in yaml.safe_load_all(text):
                walk(doc)
            # Stored with the INI's escaped newlines, like the source.
            values = [v.replace("\\n", "\n") for v in values]
        else:
            continue
        values = [v for v in values if "START_PRINT" in v]
        if values:
            found[path.relative_to(ROOT)] = values
    return found


class NozzleGrowthTest(unittest.TestCase):
    def test_every_slicer_sends_it_after_start_print_with_its_m104_placeholder(self):
        found = start_gcodes()
        seen = {part for path in found for part in path.parts}
        self.assertEqual(FAMILIES - seen, set())
        for path, values in found.items():
            for g in values:
                with self.subTest(path=str(path)):
                    lines = [line.strip() for line in g.split("\n")]
                    self.assertEqual(lines.count("START_PRINT"), 1)
                    calls = [i for i, line in enumerate(lines)
                             if line.startswith("SVZERO_NOZZLE_GROWTH")]
                    self.assertEqual(len(calls), 1)
                    at, call = lines.index("START_PRINT"), calls[0]
                    self.assertGreater(call, at)
                    # Nothing moves or heats between START_PRINT and the call.
                    between = [line for line in lines[at + 1:call]
                               if re.match(r"[GM]\d", line)]
                    self.assertEqual(between, [])
                    m104 = next(line for line in lines[call:] if line.startswith("M104 S"))
                    self.assertEqual(lines[call], "SVZERO_NOZZLE_GROWTH TEMP=" + m104[len("M104 S"):])

    def test_published_off(self):
        # Published k is 0 until a machine measures its own; see the file.
        text = GROWTH.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?m)^variable_mm_per_c:\s*0\.0\s*$")
        self.assertRegex(text, r"(?m)^variable_max_adjust:\s*0\.25\s*$")

    def test_start_print_writes_only_declared_variables(self):
        # SET_GCODE_VARIABLE on an undeclared variable is a Klipper error,
        # which no render check sees.
        declared = set(re.findall(r"(?m)^variable_(\w+):", GROWTH.read_text(encoding="utf-8")))
        written = set(re.findall(r"MACRO=_NOZZLE_GROWTH VARIABLE=(\w+)", START.read_text(encoding="utf-8")))
        self.assertTrue(written)
        self.assertLessEqual(written, declared)

    def test_start_print_resets_first_and_records_after_calibrating(self):
        text = START.read_text(encoding="utf-8")
        reset = text.index("VARIABLE=ref_temp VALUE=-1.0")
        calibrate = text.index("Z_OFFSET_CALIBRATION METHOD=force_overlay")
        record = text.index("VARIABLE=ref_temp VALUE={growth_ref}")
        home = text.index("\n    G28 Z\n")
        self.assertLess(reset, text.index("\n    CLEAN_NOZZLE\n"))
        self.assertLess(calibrate, record)
        self.assertLess(record, home)

    def test_both_firmware_bundles_ship_it(self):
        for name in ("svzero-1.3.7.cfg", "svzero-1.4.x.cfg"):
            text = (ROOT / "bundles/klipper" / name).read_text(encoding="utf-8")
            with self.subTest(bundle=name):
                for section in ("[gcode_macro _NOZZLE_GROWTH]",
                                "[gcode_macro SVZERO_NOZZLE_GROWTH]",
                                "[gcode_macro SVZERO_NOZZLE_GROWTH_EDDY_REF]"):
                    self.assertEqual(text.count(section), 1)


if __name__ == "__main__":
    unittest.main()
