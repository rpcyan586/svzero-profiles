#!/usr/bin/env python3
"""The hotend reads as a PT1000, and the Sovol-derived temperatures move with it.

klipper/config/hotend_pt1000.cfg replaces Sovol's sensor table; tools/generate.py
rebases every Sovol-derived nozzle and first-layer temperature from
source/hotend-sensor.json (operator, 2026-10-31). PCTG, the operator's own
preset, is not rebased.
"""
import importlib.util
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("generate", ROOT / "tools/generate.py")
G = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G)

SENSOR = G.load("hotend-sensor.json")
HOTEND = ROOT / "klipper/config/hotend_pt1000.cfg"
ORCA = ROOT / "bundles/orca-vendor/SVZero/filament"

# Klipper's own LinearInterpolate (klippy/extras/adc_temperature.py at the Zero's
# fork base faa89be8), an ideal PT1000 on 10 kOhm read through Sovol's table and
# 11500: the nozzle temperature at each set point. Computed outside this repo,
# so the port in generate.py is checked against Klipper, not against itself.
KLIPPER_REFERENCE = {25: 26.491, 200: 194.855, 250: 242.182, 300: 290.232, 350: 339.041}


def option(text, name):
    return re.search(r"(?m)^%s:\s*(\S+)\s*$" % name, text).group(1)


class HotendSensorTests(unittest.TestCase):
    def test_curve_matches_klipper(self):
        for setpoint, nozzle in KLIPPER_REFERENCE.items():
            with self.subTest(setpoint=setpoint):
                self.assertAlmostEqual(G.stock_curve_true_temperature(SENSOR, setpoint), nozzle, places=2)

    def test_override_is_the_three_options(self):
        text = HOTEND.read_text(encoding="utf-8")
        body = [l for l in text.splitlines() if l and not l.startswith("#")]
        self.assertEqual(body, ["[extruder]", "sensor_type: PT1000",
                                "pullup_resistor: %d" % SENSOR["real_pullup_resistor"],
                                "max_temp: %s" % option(text, "max_temp")])

    def test_max_temp_is_the_stock_ceiling_rebased(self):
        text = HOTEND.read_text(encoding="utf-8")
        self.assertEqual(int(option(text, "max_temp")),
                         G.rebased_temperature(SENSOR, SENSOR["stock_max_temp"]))

    def test_both_firmware_files_include_it_and_the_bundles_carry_it(self):
        for fw in ("1.3.7", "1.4.x"):
            entry = (ROOT / ("klipper/config/svzero-%s.cfg" % fw)).read_text(encoding="utf-8")
            bundle = (ROOT / ("bundles/klipper/svzero-%s.cfg" % fw)).read_text(encoding="utf-8")
            with self.subTest(firmware=fw):
                self.assertEqual(entry.count("[include hotend_pt1000.cfg]"), 1)
                self.assertIn("[extruder]\nsensor_type: PT1000\n", bundle)

    def test_growth_k_is_the_measured_k_per_real_degree(self):
        growth = (ROOT / "klipper/config/nozzle_growth.cfg").read_text(encoding="utf-8")
        k = float(option(growth, "variable_mm_per_c"))
        span = (G.stock_curve_true_temperature(SENSOR, 280)
                - G.stock_curve_true_temperature(SENSOR, 150))
        measured = 1.1e-3  # runs 12 and 13, per degree on Sovol's curve
        self.assertAlmostEqual(k, measured * 130 / span, delta=5e-6)

    def test_sovol_derived_presets_are_rebased_and_pctg_is_not(self):
        source = G.sections_from_tree(G.load("presets.json"), G.load("model.json"))
        model = G.load("model.json")
        ss = G.sections_from_tree(G.load("presets.json"), model)
        G.derive(model, ss, G.load("keymap.json"))
        pctg = [s for s in ss if s.startswith("filament:") and "PCTG" in s]
        self.assertTrue(pctg)
        for sec in pctg:
            for key in SENSOR["keys"]:
                if key in source[sec]:
                    with self.subTest(section=sec, key=key):
                        self.assertEqual(ss[sec][key], source[sec][key])
        for doc_path in ORCA.glob("SV Zero *.json"):
            doc = json.loads(doc_path.read_text(encoding="utf-8"))
            with self.subTest(preset=doc_path.stem):
                if "PCTG" in doc_path.stem:
                    self.assertNotIn("[PT1000]", "".join(doc.get("filament_notes", [""])))
                elif "nozzle_temperature" in doc:
                    self.assertIn("[PT1000]", "".join(doc.get("filament_notes", [""])))


if __name__ == "__main__":
    unittest.main()
